"""Per-OS helpers. Prefer native APIs; fall back to subprocess only when needed."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = platform.system()  # Windows | Darwin | Linux
IS_WIN = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"

_NO_WINDOW = 0x08000000 if IS_WIN else 0


def run(cmd: list[str] | str, timeout: float = 15, shell: bool = False, cwd: str | None = None):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=shell, cwd=cwd,
        creationflags=_NO_WINDOW, encoding="utf-8", errors="replace",
    )


def open_path(target: str) -> None:
    """Open a file, folder or URL with the default handler."""
    if IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def home() -> Path:
    return Path.home()


def user_folders() -> dict[str, Path]:
    h = home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    out = {n.lower(): h / n for n in names}
    # OneDrive-redirected folders on Windows
    od = os.environ.get("OneDrive")
    if IS_WIN and od:
        for n in ("Desktop", "Documents", "Pictures"):
            p = Path(od) / n
            if p.exists():
                out[n.lower()] = p
    out["home"] = h
    return {k: v for k, v in out.items() if v.exists()}


def resolve_user_path(p: str) -> Path:
    """Expand ~, env vars and friendly names like 'downloads/report.pdf'."""
    p = os.path.expandvars(os.path.expanduser(p.strip().strip('"')))
    path = Path(p)
    if not path.is_absolute():
        first = path.parts[0].lower() if path.parts else ""
        folders = user_folders()
        if first in folders:
            path = folders[first].joinpath(*path.parts[1:])
        else:
            path = home() / path
    return path


def which(name: str) -> str | None:
    return shutil.which(name)


PY = sys.executable
