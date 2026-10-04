"""Shell commands. High risk: asks for confirmation by default and can be disabled entirely."""

from __future__ import annotations

import os
import subprocess

from .osutil import IS_WIN, resolve_user_path
from .registry import ToolError, tool

_cwd = {"path": os.path.expanduser("~")}


@tool(risk="high", tags=["command", "terminal", "shell", "run", "powershell", "bash", "cmd", "execute"],
      examples=["run_command(command='ipconfig')", "run_command(command='git status', cwd='~/projects/app')"],
      timeout=130)
def run_command(command: str, cwd: str = "", timeout: int = 60) -> dict:
    """Run a shell command (PowerShell on Windows, bash/zsh elsewhere). The working directory persists."""
    if cwd:
        p = resolve_user_path(cwd)
        if not p.is_dir():
            raise ToolError(f"Not a folder: {p}")
        _cwd["path"] = str(p)
    stripped = command.strip()
    # Support `cd` to keep a persistent working dir like a real terminal
    if stripped.startswith("cd ") and "&&" not in stripped and ";" not in stripped:
        p = resolve_user_path(stripped[3:].strip())
        if not p.is_dir():
            raise ToolError(f"No such folder: {p}")
        _cwd["path"] = str(p)
        return {"cwd": _cwd["path"], "output": ""}
    if IS_WIN:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        args = [os.environ.get("SHELL", "/bin/bash"), "-lc", command]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=max(1, min(timeout, 120)),
                           cwd=_cwd["path"], encoding="utf-8", errors="replace",
                           creationflags=0x08000000 if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s.", hint="Use a longer timeout or a faster command.")
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    return {"exit_code": r.returncode, "cwd": _cwd["path"], "output": out[-8000:]}
