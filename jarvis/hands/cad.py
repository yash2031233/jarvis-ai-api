"""3D CAD tool. Tool name `print3d`."""

from __future__ import annotations

from ..cad import designer, openscad
from .osutil import open_path
from .registry import ToolError, tool

PRINT_ACTIONS = {"slice", "send", "slots", "status", "pause", "resume", "cancel"}


@tool(
    "print3d",
    description=(
        "Design 3D-printable parts in OpenSCAD and show them in the 3D view on Jarvis's screen. actions: "
        "design = make a new part from a description (give `what` with all sizes/constraints and a short `name`); "
        "tweak = change the part on screen or `name` ('make the hole 5mm wider', 'taller', 'add 2 screw holes'), "
        "saved as a new version; view = open a part in the 3D view; preview = render images (`angle`: iso, front, "
        "back, left, right, top, bottom, or several comma-separated); list = parts made so far; "
        "versions = a part's history; revert = bring back `version` of a part; "
        "export = the part's STL/3MF/SCAD files and open its folder. "
        "Every design/tweak is compiled, auto-fixed if OpenSCAD errors, and checked (size vs the bed, watertight) - "
        "read `issues` in the result and tweak again if something is wrong."
    ),
    risk="low", readonly=False, timeout=900,
    tags=["3d", "print", "cad", "design", "model", "openscad", "part", "bracket", "holder", "hook", "stl", "printer"],
    examples=["print3d(action='design', what='wall hook for headphones, 35 mm wide, two M4 screw holes', "
              "name='headphone hook')", "print3d(action='tweak', change='make it 10 mm wider')"],
)
async def print3d(action: str, name: str = "", what: str = "", change: str = "", angle: str = "iso",
                  version: int = 0) -> dict:
    a = action.lower().strip()
    try:
        if a == "design":
            if not what.strip():
                raise ToolError("Describe the part in `what`.", hint="Include sizes and what it attaches to.")
            return await designer.design(what, name)
        if a == "tweak":
            if not change.strip():
                raise ToolError("Say what to change in `change`.")
            return await designer.tweak(name, change)
        if a == "view":
            part = designer.resolve(name)
            vs = designer.versions_of(part)
            v = version if version in vs else vs[-1]
            p = designer.show(part, v)
            return {"part": part, "version": v, "size_mm": p.get("size_mm"), "shown": "3D view on Jarvis's screen"}
        if a == "preview":
            angles = [x.strip() for x in angle.split(",") if x.strip()] or ["iso"]
            bad = [x for x in angles if x not in openscad.ANGLES]
            if bad:
                raise ToolError(f"Unknown angle {bad}.", hint=f"Use: {', '.join(openscad.ANGLES)}")
            return {"images": designer.preview(name, angles)}
        if a == "list":
            return {"parts": designer.parts() or "none yet"}
        if a in ("versions", "history"):
            return {"part": designer.resolve(name), "versions": designer.history(name)}
        if a in ("revert", "undo"):
            if not version:
                raise ToolError("Give the `version` to bring back.", hint="Use action=versions to see them.")
            return designer.revert(name, version)
        if a == "export":
            part = designer.resolve(name)
            v = designer.versions_of(part)[-1]
            d = designer.vdir(part, v)
            open_path(str(d))
            return {"part": part, "version": v, "folder": str(d),
                    "files": sorted(p.name for p in d.iterdir())}
        if a in PRINT_ACTIONS:
            raise ToolError("Printing isn't built in yet (design and view only for now).",
                            hint="Use action=export to get the STL/3MF for the slicer.")
        raise ToolError(f"Unknown action '{action}'.",
                        hint="design, tweak, view, preview, list, versions, revert, export")
    except openscad.OpenSCADMissing:
        raise ToolError("OpenSCAD isn't installed on this PC.",
                        hint="Install OpenSCAD (nightly recommended) from openscad.org, or set its path in settings.")
    except LookupError as e:
        raise ToolError(str(e), hint="Use action=list to see the parts.")
    except RuntimeError as e:
        raise ToolError(str(e)[-900:], hint="Simplify the request or describe the shape differently, then retry.")
