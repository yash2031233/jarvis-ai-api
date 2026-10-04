"""OpenSCAD runner + mesh checks.

- Finds OpenSCAD (settings → nightly → stable → PATH), uses the fast Manifold backend when available.
- BOSL2 ships with the app (jarvis/cad/lib) and is on OPENSCADPATH, so `include <BOSL2/std.scad>` works.
- Mesh checks run on the exported STL: size, triangle count, watertight (every edge shared by exactly two
  triangles), volume — so the model hears about broken geometry before the user sees it.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import config

LIB_DIR = Path(__file__).resolve().parent / "lib"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

CANDIDATES = [
    r"C:\Program Files\OpenSCAD (Nightly)\openscad.com",
    r"C:\Program Files\OpenSCAD (Nightly)\openscad.exe",
    r"C:\Program Files\OpenSCAD\openscad.com",
    r"C:\Program Files\OpenSCAD\openscad.exe",
    "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    "/Applications/OpenSCAD-nightly.app/Contents/MacOS/OpenSCAD",
    "/usr/bin/openscad", "/usr/local/bin/openscad", "/snap/bin/openscad",
]

# Camera presets for PNG previews: --camera=tx,ty,tz,rx,ry,rz,dist (dist ignored with --viewall)
ANGLES = {
    "iso": "0,0,0,65,0,205,0",
    "iso2": "0,0,0,65,0,295,0",
    "front": "0,0,0,90,0,0,0",
    "back": "0,0,0,90,0,180,0",
    "left": "0,0,0,90,0,270,0",
    "right": "0,0,0,90,0,90,0",
    "top": "0,0,0,0,0,0,0",
    "bottom": "0,0,0,180,0,0,0",
}


class OpenSCADMissing(RuntimeError):
    pass


@functools.lru_cache(maxsize=4)
def _detect(configured: str) -> tuple[str, bool]:
    paths = [configured] if configured else []
    paths += CANDIDATES
    found = next((p for p in paths if p and Path(p).exists()), None) or shutil.which("openscad")
    if not found:
        raise OpenSCADMissing("OpenSCAD isn't installed.")
    try:
        help_text = subprocess.run([found, "--help"], capture_output=True, text=True, timeout=20,
                                   creationflags=_NO_WINDOW)
        manifold = "backend" in (help_text.stdout + help_text.stderr).lower()
    except Exception:
        manifold = False
    return found, manifold


def binary() -> tuple[str, bool]:
    return _detect(config.store.load().openscad_path or "")


def _cmd(*args: str) -> list[str]:
    exe, manifold = binary()
    return [exe, *(["--backend=Manifold"] if manifold else []), *args]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["OPENSCADPATH"] = str(LIB_DIR) + os.pathsep + env.get("OPENSCADPATH", "")
    return env


@dataclass
class Build:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: str = ""


_ERR = re.compile(r"^(ERROR|WARNING|TRACE):.*$", re.M)
# warnings that mean the geometry is wrong, not just stylistic
_BAD_WARN = re.compile(r"(Ignoring unknown (variable|function|module)|undefined operation|not specified as parameter|"
                       r"Unable to convert|undefined|"
                       r"Current top level object is empty|no top level geometry|Can't open include|"
                       r"Can't open library|wrong number of|unable to convert|too many unnamed|failed)", re.I)


def _messages(log: str, scad_name: str) -> tuple[list[str], list[str]]:
    """ERROR/WARNING blocks (they can span several lines, e.g. BOSL2 assertions), each followed by the TRACE
    lines that point into the part's own file - that's what tells the model which line to fix."""
    errors: list[str] = []
    warnings: list[str] = []
    cur: list[str] = []
    kind = ""
    noise = re.compile(r"^(Compiling|Rendering|Geometries|CGAL|Total|Normalized|Parsing|Used file cache|Saving|"
                       r"Manifold|Top level|Simple:|Vertices|Halfedges|Edges|Faces|Facets|Volumes|Status|"
                       r"Geometry cache|Polyhedrons|Genus|Top level object)")

    def flush() -> None:
        if cur:
            (errors if kind == "ERROR" else warnings).append("\n".join(cur)[:900])

    for raw in log.splitlines():
        ln = raw.rstrip()
        m = re.match(r"^(ERROR|WARNING|TRACE|ECHO|DEPRECATED):", ln)
        if m and m.group(1) in ("ERROR", "WARNING"):
            flush()
            cur, kind = [ln], m.group(1)
        elif m and m.group(1) == "TRACE":
            if cur and scad_name in ln and len(cur) < 6:   # the call chain inside the part's own file
                cur.append("  " + ln)
        elif m or not ln.strip() or noise.match(ln.strip()):
            continue
        elif cur and len(cur) < 6:
            cur.append("  " + ln.strip())                     # continuation of a multi-line message
    flush()
    return errors, warnings


def run(scad: Path, out: Path, *extra: str, timeout: int = 300) -> Build:
    """Export `scad` to `out` (.stl / .3mf / .png...)."""
    try:
        p = subprocess.run(_cmd("-o", str(out), *extra, str(scad)), capture_output=True, text=True,
                           timeout=timeout, env=_env(), cwd=str(scad.parent), creationflags=_NO_WINDOW,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return Build(False, [f"OpenSCAD took longer than {timeout}s (the model is too complex or $fn too high)"])
    log = (p.stdout or "") + (p.stderr or "")
    errors, warnings = _messages(log, scad.name)
    bad = [w for w in warnings if _BAD_WARN.search(w)]
    if "top level object is empty" in log.lower() or "no top level geometry" in log.lower():
        bad.append("The design produced no geometry (the top-level object is empty).")
    ok = p.returncode == 0 and out.exists() and out.stat().st_size > 0 and not errors and not bad
    return Build(ok, errors + bad, [w for w in warnings if w not in bad], log[-4000:])


def render_png(scad: Path, out: Path, angle: str = "iso", size: tuple[int, int] = (1200, 900)) -> bool:
    out.unlink(missing_ok=True)
    cam = ANGLES.get(angle, ANGLES["iso"])
    b = run(scad, out, f"--imgsize={size[0]},{size[1]}", "--autocenter", "--viewall", f"--camera={cam}",
            "--projection=p", "--colorscheme=Tomorrow Night", timeout=300)
    return out.exists() and out.stat().st_size > 0 and not b.errors


# ------------------------------------------------------------------ mesh checks

def read_stl(path: Path) -> np.ndarray:
    """Triangles as an (N, 3, 3) float array; handles ASCII and binary STL."""
    data = path.read_bytes()
    if data[:5] == b"solid" and b"facet" in data[:1000]:
        nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        v = np.array(nums, dtype=np.float64)
        return v.reshape(-1, 3, 3)
    n = struct.unpack("<I", data[80:84])[0]
    rec = np.frombuffer(data, dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]), count=n, offset=84)
    return rec["v"].astype(np.float64).reshape(-1, 3, 3)


def mesh_report(path: Path, bed: list[int] | tuple[int, ...] = (220, 220, 220)) -> dict:
    tris = read_stl(path)
    if len(tris) == 0:
        return {"triangles": 0, "size_mm": [0, 0, 0], "watertight": False, "issues": ["The mesh is empty."]}
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(0), pts.max(0)
    size = [round(float(x), 1) for x in hi - lo]
    # watertight: quantize vertices, count undirected edges — each must be used exactly twice
    q = np.round(pts / 1e-4).astype(np.int64)
    _, vid = np.unique(q, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3).astype(np.int64)
    e = np.concatenate([vid[:, [0, 1]], vid[:, [1, 2]], vid[:, [2, 0]]])
    e.sort(axis=1)
    n = int(vid.max()) + 1
    _, counts = np.unique(e[:, 0] * n + e[:, 1], return_counts=True)
    open_edges = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    watertight = open_edges == 0
    # volume (divergence theorem) — meaningful when watertight
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    volume = float(abs(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum()) / 6.0)
    issues = []
    if not watertight:
        issues.append(f"Not watertight: {open_edges} open edges (holes in the surface) - slicers may fail.")
    if nonmanifold:
        issues.append(f"{nonmanifold} non-manifold edges (touching/overlapping solids) - union them or add clearance.")
    over = [ax for ax, s, b in zip("XYZ", size, bed) if s > b]
    if over:
        issues.append(f"Bigger than the {bed[0]}x{bed[1]}x{bed[2]} mm bed along {', '.join(over)}.")
    if min(size) < 0.8:
        issues.append(f"Very thin overall ({min(size)} mm) - may not print.")
    return {
        "triangles": int(len(tris)),
        "size_mm": size,
        # where the solid actually is - lets the model spot features placed outside the part
        "bounds_mm": {ax: [round(float(lo[i]), 1), round(float(hi[i]), 1)] for i, ax in enumerate("xyz")},
        "volume_cm3": round(volume / 1000, 2),
        "watertight": watertight,
        "fits_bed": not over,
        "issues": issues,
    }
