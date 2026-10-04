"""Slicing and printing: the part Jarvis designed -> gcode -> your 3D printer, over the network.

Slicer: any OrcaSlicer-family app (OrcaSlicer, Flashforge's Orca / Flash Studio, Bambu Studio) - found
automatically or set in `slicer_path`. Its own printer profiles are used: `printer_model` names the machine profile
(e.g. "Flashforge AD5X 0.4 nozzle"), and the matching process (draft / standard / fine) and filament profiles are
picked from the same vendor folder.

Printers (`printer_kind`):
  flashforge  Flashforge LAN (AD5X, Adventurer 5M...): the HTTP API on port 8898 with the printer's serial number and
              check code (keychain: printer_serial, printer_code) - status, material station, pause / resume /
              cancel, upload .3mf so the printer's screen shows the part; the older text protocol on 8899 as fallback.
              Found on the network by itself when `printer_host` is empty.
  moonraker   Klipper printers (Moonraker API, port 7125) - optional API key in the keychain (printer_api_key)
  octoprint   OctoPrint - API key in the keychain (printer_api_key)
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .. import config

QUALITY = {"draft": 0.24, "standard": 0.20, "fine": 0.16}
FF_LEGACY, FF_API = 8899, 8898


class PrinterError(Exception):
    pass


# ------------------------------------------------------------------ slicer
def slicer_path() -> Path:
    s = config.store.load()
    cands = [s.slicer_path] if s.slicer_path else []
    if sys.platform == "win32":
        pf = [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
        cands += [str(p / rel) for p in pf for rel in (
            r"OrcaSlicer\orca-slicer.exe", r"Flashforge\Flash Studio Desktop\flash studio.exe",
            r"Orca-Flashforge\Orca-Flashforge.exe", r"Bambu Studio\bambu-studio.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
                  "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"]
    for name in ("orca-slicer", "OrcaSlicer", "bambu-studio"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    raise PrinterError("No slicer found. Install OrcaSlicer (or your printer's Orca-based slicer), or set its path "
                       "in Settings → 3D printing.")


def _profiles_root(exe: Path) -> Path:
    for root in (exe.parent / "resources" / "profiles", exe.parent.parent / "Resources" / "profiles",
                 exe.parent / "resources" / "profiles_template"):
        if root.is_dir():
            return root
    raise PrinterError(f"Can't find the slicer's printer profiles next to {exe}.")


def _lead_float(name: str) -> float:
    m = re.match(r"([\d.]+)mm", name)
    return float(m.group(1)) if m else 0.0


def profiles(quality: str = "standard") -> dict[str, Path]:
    """machine / process / filament profile files for the configured printer."""
    s = config.store.load()
    model = (s.printer_model or "").strip()
    if not model:
        raise PrinterError("Which printer? Set `printer_model` (e.g. 'Flashforge AD5X 0.4 nozzle') in Settings → "
                           "3D printing - it's the printer's name as the slicer lists it.")
    root = _profiles_root(slicer_path())
    machine = next(iter(sorted(root.glob(f"*/machine/{model}.json"))), None)
    if machine is None:
        machine = next((p for p in sorted(root.glob("*/machine/*.json")) if p.stem.lower() == model.lower()), None)
    if machine is None:
        raise PrinterError(f"The slicer has no printer profile called '{model}'.")
    vendor = machine.parent.parent
    nozzle_m = re.search(r"([\d.]+) nozzle", model)
    nozzle = nozzle_m.group(1) if nozzle_m else "0.4"
    words = [w for w in re.sub(r"[\d.]+ nozzle", "", model).split()[1:] if w]   # "Flashforge AD5X ..." -> ["AD5X"]
    token = " ".join(words) or model

    def fits(p: Path) -> bool:
        n = p.stem
        if token.lower() not in n.lower():
            return False
        return f"{nozzle} nozzle" in n if nozzle != "0.4" else "nozzle" not in n or "0.4 nozzle" in n

    procs = [p for p in vendor.glob("process/*.json") if fits(p)]
    if not procs:
        raise PrinterError(f"No print-quality profiles for {model} in the slicer.")
    target = QUALITY.get(quality.lower(), QUALITY["standard"])
    process = min(procs, key=lambda p: (abs(_lead_float(p.stem) - target), "draft" in p.stem.lower()))
    fils = [p for p in vendor.glob("filament/*.json") if fits(p)]
    want = (s.printer_filament or "PLA").lower()
    filament = (next((p for p in fils if want in p.stem.lower()), None)
                or next((p for p in fils if "pla" in p.stem.lower()), None) or (fils[0] if fils else None))
    if filament is None:
        raise PrinterError(f"No filament profile for {model} in the slicer.")
    return {"machine": machine, "process": process, "filament": filament}


def slice_part(three_mf: Path, outdir: Path, quality: str = "standard") -> dict[str, Any]:
    exe = slicer_path()
    prof = profiles(quality)
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("*.gcode"):
        f.unlink()
    sliced = f"{three_mf.stem}_sliced.3mf"
    cmd = [str(exe), "--load-settings", f"{prof['machine']};{prof['process']}", "--load-filaments", str(prof["filament"]),
           "--slice", "0", "--export-3mf", sliced, "--outputdir", str(outdir), str(three_mf)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    gcodes = sorted(outdir.glob("*.gcode"))
    if not gcodes:
        raise PrinterError(f"Slicing failed: {(r.stdout + r.stderr)[-500:]}")
    g = gcodes[0]
    head = g.read_text("utf-8", errors="replace")[:60000]

    def find(pat: str):
        m = re.search(pat, head)
        return m.group(1).strip() if m else None
    grams = find(r"total filament used \[g\] = ([\d.]+)")
    cm3 = find(r"filament used \[cm3\] = ([\d.]+)")
    if (not grams or float(grams) == 0) and cm3:      # some profiles carry no density: PLA ~1.24 g/cm3
        dens = 1.04 if "abs" in prof["filament"].stem.lower() else 1.27 if "petg" in prof["filament"].stem.lower() else 1.24
        grams = f"{float(cm3) * dens:.1f}"
    return {"gcode": str(g), "sliced_3mf": str(outdir / sliced) if (outdir / sliced).exists() else None,
            "quality": quality, "profile": prof["process"].stem, "filament": prof["filament"].stem,
            "time": find(r"estimated printing time \(normal mode\) = (.+)"),
            "grams": grams,
            "cost": find(r"total filament cost = ([\d.]+)"),
            "height_mm": find(r"max_z_height: ([\d.]+)")}


# ------------------------------------------------------------------ finding the printer
def _probe(ip: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _subnet() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return ".".join(s.getsockname()[0].split(".")[:3])
    finally:
        s.close()


def host() -> str:
    """The printer's address: the saved one if it answers, else a quick sweep of the local network (and saved)."""
    s = config.store.load()
    port = {"flashforge": FF_API, "moonraker": 7125, "octoprint": 80}.get(s.printer_kind, FF_API)
    if s.printer_host and (_probe(s.printer_host, port, 1.0) or _probe(s.printer_host, FF_LEGACY, 1.0)):
        return s.printer_host
    # not where it was (or never set): find it again - only a device that identifies itself as a printer counts
    from .. import devices

    found = devices.scan(include_cameras=False)
    devices.apply(found)
    s = config.store.load()
    if s.printer_host and any(p["host"] == s.printer_host for p in found["printers"]):
        return s.printer_host
    raise PrinterError("No 3D printer answering on the network - is it switched on, with LAN mode enabled?")


# ------------------------------------------------------------------ Flashforge
def _ff_body(extra: dict | None = None) -> dict:
    serial, code = config.get_secret("printer_serial"), config.get_secret("printer_code")
    if not (serial and code):
        raise PrinterError("The Flashforge needs its serial number and check code (printer screen → Settings → "
                           "network / about). Add them in Settings → 3D printing.")
    return {"serialNumber": serial, "checkCode": code, **(extra or {})}


def _ff(path: str, extra: dict | None = None) -> dict:
    r = httpx.post(f"http://{host()}:{FF_API}{path}", json=_ff_body(extra), timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise PrinterError(f"Printer: {d.get('message')}")
    return d


NAMED = {"black": (0x16, 0x16, 0x16), "white": (0xFF, 0xFF, 0xFF), "red": (0xF7, 0x22, 0x24),
         "blue": (0x27, 0x50, 0xE0), "green": (0x00, 0xAE, 0x42), "yellow": (0xF4, 0xEE, 0x2A),
         "orange": (0xFF, 0x6A, 0x13), "grey": (0x80, 0x80, 0x80), "gray": (0x80, 0x80, 0x80),
         "purple": (0x81, 0x1A, 0xC3), "pink": (0xF5, 0x54, 0x9A)}


def _rgb(hexstr: str):
    h = (hexstr or "").lstrip("#")[:6]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else None


def color_name(hexstr: str) -> str:
    rgb = _rgb(hexstr)
    if not rgb:
        return "?"
    return min(NAMED, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, NAMED[n])))


def slots() -> list[dict]:
    """What's loaded in the material station, with names for the colours."""
    d = (_ff("/detail").get("detail") or {}).get("matlStationInfo") or {}
    return [{"slot": s.get("slotId"), "color": s.get("materialColor"), "name": color_name(s.get("materialColor", "")),
             "material": s.get("materialName"), "loaded": s.get("hasFilament")} for s in d.get("slotInfos", [])]


def slot_for_color(want: str) -> dict:
    st = slots()
    if not st:
        raise PrinterError("No material station info.")
    w = (want or "").strip().lower()
    if w.isdigit():
        hit = next((s for s in st if s["slot"] == int(w)), None)
        if hit:
            return hit
        raise PrinterError(f"No slot {w}.")
    if w.startswith("#") and _rgb(w):
        t = _rgb(w)
        return min(st, key=lambda s: sum((a - b) ** 2 for a, b in zip(_rgb(s["color"]) or (0, 0, 0), t)))
    hit = next((s for s in st if s["name"] == w), None)
    if hit:
        return hit
    raise PrinterError(f"No {want} loaded - slots: " + ", ".join(f"{s['slot']}={s['name']}" for s in st))


# ------------------------------------------------------------------ status / control / upload (all kinds)
def _kind() -> str:
    k = config.store.load().printer_kind
    if not k:
        raise PrinterError("No printer set up. Settings → 3D printing: pick the printer type (Flashforge, Klipper / "
                           "Moonraker or OctoPrint).")
    return k


def _key_headers() -> dict:
    k = config.get_secret("printer_api_key")
    return {"X-Api-Key": k} if k else {}


def status() -> dict[str, Any]:
    k = _kind()
    if k == "flashforge":
        d = _ff("/detail").get("detail", {})
        out = {"state": d.get("status"), "file": d.get("printFileName"),
               "layer": f"{d.get('printLayer')}/{d.get('targetPrintLayer')}",
               "progress_pct": round((d.get("printProgress") or 0) * 100, 1),
               "minutes_left": round((d.get("estimatedTime") or 0) / 60),
               "nozzle_c": d.get("rightTemp"), "bed_c": d.get("platTemp"), "error": d.get("errorCode") or None}
        try:
            out["slots"] = slots()
        except Exception:
            pass
        return out
    if k == "moonraker":
        r = httpx.get(f"http://{host()}:7125/printer/objects/query?print_stats&display_status&extruder&heater_bed",
                      headers=_key_headers(), timeout=8).json()["result"]["status"]
        ps = r.get("print_stats", {})
        return {"state": ps.get("state"), "file": ps.get("filename"),
                "progress_pct": round((r.get("display_status", {}).get("progress") or 0) * 100, 1),
                "nozzle_c": r.get("extruder", {}).get("temperature"), "bed_c": r.get("heater_bed", {}).get("temperature")}
    r = httpx.get(f"http://{host()}/api/job", headers=_key_headers(), timeout=8).json()
    return {"state": r.get("state"), "file": (r.get("job") or {}).get("file", {}).get("name"),
            "progress_pct": round((r.get("progress") or {}).get("completion") or 0, 1),
            "minutes_left": round(((r.get("progress") or {}).get("printTimeLeft") or 0) / 60)}


def control(action: str) -> dict[str, Any]:
    """pause | resume | cancel"""
    k = _kind()
    if k == "flashforge":
        _ff("/control", {"payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "",
                                                                    "action": {"resume": "continue"}.get(action, action)}}})
    elif k == "moonraker":
        httpx.post(f"http://{host()}:7125/printer/print/{action}", headers=_key_headers(), timeout=8).raise_for_status()
    else:
        body = {"command": "cancel"} if action == "cancel" else {"command": "pause", "action": action}
        httpx.post(f"http://{host()}/api/job", json=body, headers=_key_headers(), timeout=8).raise_for_status()
    time.sleep(2)
    return {"done": action, "now": status().get("state")}


def send(path: Path, start: bool = True, slot: int | None = None) -> dict[str, Any]:
    k = _kind()
    data = path.read_bytes()
    if k == "flashforge":
        fields = {**_ff_body(), "fileSize": str(len(data)), "printNow": "true" if start else "false",
                  "levelingBeforePrint": "false", "flowCalibration": "false", "useMatlStation": "true",
                  "gcodeToolCnt": "1",
                  "materialMappings": json.dumps([{"toolId": 0, "slotId": slot, "materialName": "PLA",
                                                   "toolMaterialColor": "", "slotMaterialColor": ""}]) if slot else "[]"}
        r = httpx.post(f"http://{host()}:{FF_API}/uploadGcode", data=fields,
                       files={"gcodeFile": (path.name, data, "application/octet-stream")}, timeout=300)
        d = r.json()
        if d.get("code") != 0:
            raise PrinterError(f"Printer refused the file: {d.get('message')}")
        return {"sent": path.name, "bytes": len(data), "started": start, "slot": slot}
    if k == "moonraker":
        r = httpx.post(f"http://{host()}:7125/server/files/upload", headers=_key_headers(), timeout=300,
                       files={"file": (path.name, data)}, data={"print": "true" if start else "false"})
        r.raise_for_status()
        return {"sent": path.name, "bytes": len(data), "started": start}
    r = httpx.post(f"http://{host()}/api/files/local", headers=_key_headers(), timeout=300,
                   files={"file": (path.name, data)}, data={"print": "true" if start else "false"})
    r.raise_for_status()
    return {"sent": path.name, "bytes": len(data), "started": start}
