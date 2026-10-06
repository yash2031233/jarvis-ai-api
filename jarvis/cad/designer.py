"""Design loop: description → OpenSCAD (by the model) → compile → fix errors → check mesh → version → show.

Every part lives in <data>/cad/<part>/v<N>/ (part.scad, part.stl, part.3mf, preview.png, meta.json).
Every tweak is a new version, so any earlier one can be brought back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .. import config
from ..brain.client import brain
from ..events import bus
from . import openscad

log = logging.getLogger(__name__)

CAD_DIR = config.DATA_DIR / "cad"
STATE = CAD_DIR / "state.json"
MAX_FIX = 5

DESIGN_SYSTEM = """You write OpenSCAD for functional 3D-printed parts. Output ONLY OpenSCAD code, no prose, no fences.

Rules:
- Put every dimension in a named variable at the top with a short comment, so it can be tweaked later.
- Design for FDM printing: flat bottom on the bed (z = 0 up), avoid overhangs steeper than ~45 degrees,
  no floating parts, minimum wall 2 mm, minimum feature 1.5 mm. Add clearance (0.4-0.8 mm) anywhere two
  things fit together.
- Real, printable sizes in millimetres. If the user gives a measurement, use it exactly; otherwise use a sane
  standard size and note it in a comment.
- Prefer one solid piece. Use difference() for holes and slots, hull() for rounded shapes. Make cutting
  shapes 0.01-1 mm longer than the wall so cuts go all the way through (no zero-thickness skins).
- Keep $fn reasonable ($fn = 64 for round parts). No text() unless asked. No imported files.
- Use plain OpenSCAD only (cube, cylinder, sphere, hull, linear_extrude, rotate_extrude, polygon, difference,
  union, translate, rotate, mirror). Do not include libraries unless the request asks for BOSL2.
  Only use arguments those built-ins actually have (e.g. linear_extrude(height=..), cylinder(h=.., d=.. or
  d1=.., d2=..), never both r and d). Every module you call must be defined in the file.
- Countersunk screw hole: a cylinder(d=clearance) through the plate plus a cylinder(h=head_depth,
  d1=clearance, d2=head_d) cone at the surface the screw head sits on. M3: 3.4 / 6.5 mm, M4: 4.5 / 8.5 mm,
  M5: 5.5 / 10.5 mm (clearance / head diameter).

START THE FILE WITH A PLAN as // comments, before any code:
  // PLAN
  // overall size: X x Y x Z mm (X = width, Y = depth, Z = height on the bed)
  // orientation: which face sits on the bed and why it prints without supports
  // features: one line each - what it is, its size, and where it is (x/y/z ranges)
Then write the code so it follows the plan exactly. The finished part must measure about the planned size.
- Keep it under 100 lines.

Think about the ORIENTATION and the SHAPE before writing:
- A clip/clamp that grips an edge is a C or U profile: the slot must be OPEN on one side so the edge can enter.
  Cutting a slot that does not reach an outside face gives you a closed box - wrong part.
- Sketch the cross-section in your head, then extrude it. For a C-clip: two arms and a spine, slot open at the front.
- Hooks, brackets and holders are usually an L or C profile, not a box.
- After writing, re-read it once: is every feature reachable from outside? Would this actually do the job?"""

TWEAK_SYSTEM = DESIGN_SYSTEM + """

You are CHANGING an existing part. Keep everything that wasn't asked to change (same variable names, same
structure); change only what the request asks. Output the COMPLETE updated OpenSCAD file."""


# ------------------------------------------------------------------ helpers

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:48] or "part"


def part_dir(part: str) -> Path:
    return CAD_DIR / slug(part)


def versions_of(part: str) -> list[int]:
    d = part_dir(part)
    if not d.exists():
        return []
    return sorted(int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit() and (p / "part.stl").exists())


def vdir(part: str, v: int) -> Path:
    return part_dir(part) / f"v{v}"


def read_meta(part: str, v: int) -> dict[str, Any]:
    try:
        return json.loads((vdir(part, v) / "meta.json").read_text("utf-8"))
    except Exception:
        return {}


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(**kw: Any) -> None:
    CAD_DIR.mkdir(parents=True, exist_ok=True)
    s = load_state()
    s.update(kw)
    STATE.write_text(json.dumps(s), "utf-8")


def stl_url(part: str, v: int) -> str:
    f = vdir(part, v) / "part.stl"
    t = int(f.stat().st_mtime) if f.exists() else 0
    return f"/api/cad/file/{slug(part)}/{v}/part.stl?t={t}"


def show(part: str, v: int | None = None, status: str = "ready", text: str = "") -> dict[str, Any]:
    """Open the part in the model view on every connected screen."""
    part = slug(part)
    vs = versions_of(part)
    if v is None and vs:
        v = vs[-1]
    payload: dict[str, Any] = {"part": part, "name": part.replace("_", " "), "status": status, "text": text}
    if v is not None and (vdir(part, v) / "part.stl").exists():
        meta = read_meta(part, v)
        payload.update(version=v, versions=vs, url=stl_url(part, v), size_mm=meta.get("size_mm"),
                       issues=meta.get("issues", []))
    save_state(part=part, version=payload.get("version"), open=True, status=status, text=text)
    bus.emit("model", **payload)
    return payload


def _clean_code(text: str) -> str:
    m = re.search(r"```(?:openscad|scad|c)?\s*(.*?)```", text, flags=re.S)
    code = (m.group(1) if m else text).strip()
    lines = code.splitlines()
    for j, ln in enumerate(lines):  # drop prose before the first real line of code
        if re.match(r"\s*(//|/\*|include|use|[A-Za-z_$][A-Za-z0-9_]*\s*=|module\s|function\s|difference|union|"
                    r"intersection|cube|cylinder|sphere|hull|translate|rotate|linear_extrude|rotate_extrude|"
                    r"cuboid|cyl|\$fn)", ln):
            return "\n".join(lines[j:]).strip()
    return code


def _looks_like_scad(code: str) -> bool:
    if len(code) < 40:
        return False
    if re.search(r"(cube|cylinder|sphere|polyhedron|linear_extrude|rotate_extrude|hull|minkowski|difference|union|"
                 r"cuboid|cyl|tube|prismoid)\s*\(", code):
        return True
    return bool(re.search(r"^\s*[A-Za-z_]\w*\s*=\s*[-\d.]", code, flags=re.M)) and code.count(";") >= 3


async def _ask_code(messages: list[dict[str, str]]) -> str:
    max_tokens = 5000
    last = ""
    for i in range(3):
        text, finish = await brain.complete(messages, max_tokens=max_tokens, temperature=0.2 + 0.1 * i, patience=None)
        if finish == "length":
            max_tokens = min(max_tokens * 2, 12000)
        code = _clean_code(text)
        if _looks_like_scad(code):
            return code
        last = text[:300]
        messages = messages + [
            {"role": "assistant", "content": text[:600]},
            {"role": "user", "content": "That was not OpenSCAD code. Reply with the OpenSCAD source only - start at "
                                        "the first variable or comment, no explanation, no questions."},
        ]
    raise RuntimeError(f"The model would not produce OpenSCAD (its reply started: {last!r})")


def _quote_lines(code: str, problems: str) -> str:
    """Show the model the exact lines of ITS file the errors point at (line numbers alone get miscounted)."""
    lines = code.splitlines()
    nums = sorted({int(n) for n in re.findall(r"part\.scad, line (\d+)", problems)})[:8]
    shown = [f"{n:>3}: {lines[n - 1]}" for n in nums if 0 < n <= len(lines)]
    return ("\n\nThe lines those errors point at:\n" + "\n".join(shown)) if shown else ""


def _build(d: Path) -> tuple[openscad.Build, dict[str, Any] | None]:
    scad, stl = d / "part.scad", d / "part.stl"
    stl.unlink(missing_ok=True)
    b = openscad.run(scad, stl)
    if not b.ok:
        return b, None
    report = openscad.mesh_report(stl, config.store.load().bed_mm)
    if report["triangles"] == 0:
        b.ok = False
        b.errors.append("The design produced no geometry.")
        return b, None
    return b, report


async def _compile_with_fixes(d: Path, messages: list[dict[str, str]], code: str,
                              part: str) -> tuple[str, dict[str, Any], list[str], int]:
    """Write → compile → (errors → ask the model to fix) up to MAX_FIX times."""
    for attempt in range(MAX_FIX + 1):
        (d / "part.scad").write_text(code, "utf-8")
        b, report = await asyncio.to_thread(_build, d)
        # keep every attempt so a failure can be inspected later
        (d / f"attempt{attempt}.scad").write_text(code, "utf-8")
        with (d / "attempts.log").open("a", encoding="utf-8") as fh:
            status = "ok" if b.ok else "FAILED"
            fh.write(f"--- attempt {attempt}: {status}\n" + "\n".join(b.errors) + "\n")
        log.info("cad %s attempt %d: %s %s", part, attempt, "ok" if b.ok else "failed", b.errors[:2])
        if b.ok and report is not None:
            return code, report, b.warnings, attempt
        if attempt == MAX_FIX:
            # best effort: if it still produced a real solid (only warnings left), keep it and say so
            stl = d / "part.stl"
            if stl.exists() and stl.stat().st_size > 0 and not any(e.startswith("ERROR") for e in b.errors):
                rep = openscad.mesh_report(stl, config.store.load().bed_mm)
                if rep["triangles"]:
                    rep["issues"] = rep["issues"] + [f"OpenSCAD warning: {e.splitlines()[0]}" for e in b.errors[:4]]
                    return code, rep, b.warnings, attempt
            raise RuntimeError("OpenSCAD couldn't build it:\n" + "\n".join(b.errors[:8]))
        problems = ("\n".join(b.errors[:10]) or b.log[-1500:]) + _quote_lines(code, "\n".join(b.errors))
        show(part, status="building", text=f"Fixing a build error (try {attempt + 1}/{MAX_FIX})")
        messages = messages + [
            {"role": "assistant", "content": code},
            {"role": "user", "content": "OpenSCAD failed to build that:\n" + problems +
             "\n\nFix it. Output the COMPLETE corrected OpenSCAD file only."},
        ]
        code = await _ask_code(messages)
    raise RuntimeError("unreachable")


REVIEW_SYSTEM = """You inspect 3D-printable parts designed in OpenSCAD. You get the request, the measured size and
three renders of the finished part (left: iso view, middle: front view looking at +Y, right: right side view).
Judge the SHAPE against the request: are all requested features there (holes, slots, lips, arms...), in the right
place and orientation, at roughly the right size, printable flat on the bed (Z up)? Holes must go all the way
through what they cut. Ignore colour and small cosmetic details.
Reply with JSON only: {"matches": true|false, "problems": ["short, specific, fixable problem", ...]}"""

_vision_ok: bool | None = None  # learned at runtime: does the current model accept images?


def _views_sheet(d: Path) -> Path | None:
    """Iso + front + right renders side by side in one image (one image = cheaper, works on more models)."""
    shots = []
    for a in ("iso", "front", "right"):
        f = d / f"review_{a}.png"
        if openscad.render_png(d / "part.scad", f, a, size=(640, 520)):
            shots.append(f)
    if not shots:
        return None
    try:
        from PIL import Image

        imgs = [Image.open(f).convert("RGB") for f in shots]
        sheet = Image.new("RGB", (sum(i.width for i in imgs), max(i.height for i in imgs)), (30, 30, 30))
        x = 0
        for i in imgs:
            sheet.paste(i, (x, 0))
            x += i.width
        out = d / "review_sheet.png"
        sheet.save(out)
        return out
    except Exception:
        return shots[0]


async def _review(d: Path, request: str, report: dict[str, Any]) -> list[str] | None:
    """Look at the part. None = couldn't look (no vision); [] = matches; otherwise the problems found."""
    global _vision_ok
    if _vision_ok is False or not config.store.load().cad_review:
        return None
    sheet = await asyncio.to_thread(_views_sheet, d)
    if sheet is None:
        return None
    import base64

    b64 = base64.b64encode(sheet.read_bytes()).decode()
    size = report.get("size_mm")
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": f"Request: {request}\nMeasured size (X x Y x Z): {size} mm. Does the part match?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]
    try:
        text, _ = await brain.complete(messages, max_tokens=700, temperature=0.1)
    except Exception as e:
        if any(w in str(e).lower() for w in ("image", "vision", "multimodal", "content type", "image_url")):
            _vision_ok = False
            log.info("cad review disabled: model can't take images (%s)", e)
        return None
    from ..brain.repair import loads_lenient

    try:
        j = loads_lenient(text[text.find("{"):] if "{" in text else text)
    except ValueError:
        return None
    _vision_ok = True
    if not isinstance(j, dict) or j.get("matches", True):
        return []
    return [str(p) for p in (j.get("problems") or [])][:6] or ["The part doesn't match the request."]


async def _build_and_review(d: Path, messages: list[dict[str, Any]], code: str, part: str,
                            request: str) -> tuple[str, dict[str, Any], list[str], int, list[str]]:
    """Compile (with error fixes), then look at it and fix what's visibly wrong (up to 2 rounds)."""
    code, report, warnings, fixes = await _compile_with_fixes(d, messages, code, part)
    remaining: list[str] = []
    for round_ in range(3):
        show(part, status="building", text="Checking the design")
        problems = await _review(d, request, report)
        if not problems:
            remaining = []
            break
        remaining = problems
        log.info("cad %s review round %d: %s", part, round_, problems)
        show(part, status="building", text="Fixing: " + "; ".join(problems)[:300])
        messages = messages + [
            {"role": "assistant", "content": code},
            {"role": "user", "content": "It builds, but a visual check of the result found these problems:\n- " +
             "\n- ".join(problems) + f"\nMeasured size: {report.get('size_mm')} mm. "
             f"The solid actually occupies x {report.get('bounds_mm', {}).get('x')}, "
             f"y {report.get('bounds_mm', {}).get('y')}, z {report.get('bounds_mm', {}).get('z')} - "
             "check every feature's coordinates against these (a hole placed outside them cuts nothing).\n"
             "Fix them. Output the COMPLETE corrected OpenSCAD file only."},
        ]
        new_code = await _ask_code(messages)
        try:
            code, report, warnings, more = await _compile_with_fixes(d, messages, new_code, part)
            fixes += 1 + more
        except RuntimeError:
            # the fix broke the build: keep the last good version (rewrite + rebuild it)
            (d / "part.scad").write_text(code, "utf-8")
            await asyncio.to_thread(_build, d)
            break
    report = dict(report)
    report["issues"] = list(report.get("issues", [])) + [f"Visual check: {p}" for p in remaining]
    return code, report, warnings, fixes, remaining


async def _finish(part: str, v: int, code: str, report: dict, warnings: list[str], prompt: str, kind: str,
                  fixes: int, t0: float) -> dict[str, Any]:
    d = vdir(part, v)
    # 3MF + preview in parallel (the STL already exists)
    await asyncio.gather(
        asyncio.to_thread(openscad.run, d / "part.scad", d / "part.3mf"),
        asyncio.to_thread(openscad.render_png, d / "part.scad", d / "preview.png", "iso"),
    )
    meta = {"part": part, "version": v, "kind": kind, "prompt": prompt, "created": time.time(),
            "fixes": fixes, "warnings": warnings[:10], "seconds": round(time.time() - t0, 1), **report}
    (d / "meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    show(part, v)
    return {
        "part": part, "version": v, "size_mm": report["size_mm"], "volume_cm3": report.get("volume_cm3"),
        "watertight": report["watertight"], "fits_bed": report["fits_bed"], "issues": report["issues"],
        "auto_fixes": fixes, "seconds": meta["seconds"],
        "files": {"scad": str(d / "part.scad"), "stl": str(d / "part.stl"),
                  "3mf": str(d / "part.3mf") if (d / "part.3mf").exists() else None,
                  "preview": str(d / "preview.png") if (d / "preview.png").exists() else None},
        "shown": "on Jarvis's screen (3D view)",
        "next": "If `issues` lists problems, fix them with a tweak before saying it's done.",
    }


# ------------------------------------------------------------------ actions

async def design(what: str, name: str = "") -> dict[str, Any]:
    t0 = time.time()
    part = slug(name or " ".join(re.findall(r"[a-zA-Z0-9]+", what)[:4]))
    existing = versions_of(part)
    if existing:  # don't clobber an existing part with the same name
        n = 2
        while versions_of(f"{part}_{n}"):
            n += 1
        part = f"{part}_{n}"
    d = vdir(part, 1)
    d.mkdir(parents=True, exist_ok=True)
    show(part, status="building", text=what)
    messages = [{"role": "system", "content": DESIGN_SYSTEM}, {"role": "user", "content": what}]
    try:
        code = await _ask_code(messages)
        code, report, warnings, fixes, _ = await _build_and_review(d, messages, code, part, what)
    except Exception as e:
        show(part, status="error", text=str(e)[-400:])
        raise
    return await _finish(part, 1, code, report, warnings, what, "design", fixes, t0)


async def tweak(part: str, change: str) -> dict[str, Any]:
    t0 = time.time()
    part = resolve(part)
    vs = versions_of(part)
    cur = load_state().get("version") if load_state().get("part") == part else None
    base_v = cur if cur in vs else vs[-1]
    code0 = (vdir(part, base_v) / "part.scad").read_text("utf-8")
    v = vs[-1] + 1
    d = vdir(part, v)
    d.mkdir(parents=True, exist_ok=True)
    show(part, base_v, status="building", text=change)
    messages = [
        {"role": "system", "content": TWEAK_SYSTEM},
        {"role": "user", "content": f"Current part ({part}, version {base_v}):\n\n{code0}\n\nChange: {change}"},
    ]
    try:
        code = await _ask_code(messages)
        req = (read_meta(part, base_v).get("prompt") or "") + f"\nChange requested now: {change}"
        code, report, warnings, fixes, _ = await _build_and_review(d, messages, code, part, req)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        show(part, base_v, status="error", text=str(e)[-400:])
        raise
    out = await _finish(part, v, code, report, warnings, change, f"tweak of v{base_v}", fixes, t0)
    out["based_on"] = base_v
    return out


def resolve(part: str) -> str:
    """Find a part by (fuzzy) name; empty = the one on screen."""
    from rapidfuzz import fuzz, process

    if not part:
        p = load_state().get("part")
        if p and versions_of(p):
            return p
        raise LookupError("No part is open. Say which part, or design one first.")
    s = slug(part)
    if versions_of(s):
        return s
    names = [p.name for p in CAD_DIR.glob("*") if p.is_dir() and versions_of(p.name)] if CAD_DIR.exists() else []
    hit = process.extractOne(s, names, scorer=fuzz.WRatio) if names else None
    if hit and hit[1] >= 70:
        return hit[0]
    raise LookupError(f"No part called '{part}'. Parts: {', '.join(names) or 'none yet'}")


def revert(part: str, version: int) -> dict[str, Any]:
    """Bring an old version back as the newest one (history is kept)."""
    part = resolve(part)
    vs = versions_of(part)
    if version not in vs:
        raise LookupError(f"{part} has versions {vs}")
    v = vs[-1] + 1
    shutil.copytree(vdir(part, version), vdir(part, v))
    meta = read_meta(part, v)
    meta.update(version=v, kind=f"revert to v{version}", created=time.time())
    (vdir(part, v) / "meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
    show(part, v)
    return {"part": part, "version": v, "restored_from": version, "size_mm": meta.get("size_mm")}


def history(part: str) -> list[dict[str, Any]]:
    part = resolve(part)
    out = []
    for v in versions_of(part):
        m = read_meta(part, v)
        out.append({"version": v, "kind": m.get("kind"), "request": m.get("prompt"), "size_mm": m.get("size_mm"),
                    "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("created", 0)))})
    return out


def parts() -> list[dict[str, Any]]:
    if not CAD_DIR.exists():
        return []
    rows = []
    for d in CAD_DIR.iterdir():
        vs = versions_of(d.name) if d.is_dir() else []
        if not vs:
            continue
        m = read_meta(d.name, vs[-1])
        rows.append({"part": d.name, "versions": len(vs), "latest": vs[-1], "size_mm": m.get("size_mm"),
                     "updated": m.get("created", 0)})
    rows.sort(key=lambda r: -r["updated"])
    for r in rows:
        r["updated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"]))
    return rows


def preview(part: str, angles: list[str]) -> list[str]:
    part = resolve(part)
    v = versions_of(part)[-1]
    if load_state().get("part") == part and load_state().get("version") in versions_of(part):
        v = load_state()["version"]
    shots = []
    for a in angles or ["iso"]:
        out = vdir(part, v) / f"preview_{a}.png"
        if out.exists() or openscad.render_png(vdir(part, v) / "part.scad", out, a):
            shots.append(str(out))
    return shots
