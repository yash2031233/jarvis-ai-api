"""Apps: open / close / focus / list. Uses a pre-built app index for instant lookup."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import psutil
from rapidfuzz import fuzz, process

from .osutil import IS_LINUX, IS_MAC, IS_WIN, open_path
from .registry import ToolError, tool

log = logging.getLogger(__name__)

# Common aliases → canonical names / launch targets
ALIASES = {
    "chrome": "google chrome", "vscode": "visual studio code", "vs code": "visual studio code",
    "code": "visual studio code", "word": "microsoft word", "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint", "edge": "microsoft edge", "terminal": "terminal",
    "file explorer": "explorer", "files": "explorer", "settings": "settings", "calculator": "calculator",
    "notepad": "notepad", "spotify": "spotify", "discord": "discord", "steam": "steam",
}

WIN_BUILTINS = {
    "explorer": "explorer.exe", "notepad": "notepad.exe", "calculator": "calc.exe",
    "settings": "ms-settings:", "task manager": "taskmgr.exe", "paint": "mspaint.exe",
    "terminal": "wt.exe", "command prompt": "cmd.exe", "powershell": "powershell.exe",
    "control panel": "control.exe", "snipping tool": "snippingtool.exe",
}


class AppIndex:
    def __init__(self) -> None:
        self.apps: dict[str, str] = {}  # lowercase display name -> launch target
        self._lock = threading.Lock()
        self.built_at = 0.0

    def build(self) -> None:
        apps: dict[str, str] = {}
        try:
            if IS_WIN:
                roots = [
                    Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
                    Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
                ]
                for r in roots:
                    if r.exists():
                        for f in r.rglob("*"):
                            if f.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                                name = f.stem.lower()
                                if "uninstall" in name or "readme" in name:
                                    continue
                                apps.setdefault(name, str(f))
                for k, v in WIN_BUILTINS.items():
                    apps.setdefault(k, v)
            elif IS_MAC:
                for r in [Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"]:
                    if r.exists():
                        for f in list(r.glob("*.app")) + list(r.glob("*/*.app")):
                            apps.setdefault(f.stem.lower(), str(f))
            else:
                dirs = [Path("/usr/share/applications"), Path.home() / ".local/share/applications",
                        Path("/var/lib/flatpak/exports/share/applications")]
                for r in dirs:
                    if r.exists():
                        for f in r.glob("*.desktop"):
                            try:
                                txt = f.read_text("utf-8", errors="ignore")
                            except Exception:
                                continue
                            m = re.search(r"^Name=(.+)$", txt, re.M)
                            if m and "NoDisplay=true" not in txt:
                                apps.setdefault(m.group(1).strip().lower(), str(f))
        except Exception as e:
            log.warning("app index failed: %s", e)
        with self._lock:
            self.apps = apps
            self.built_at = time.time()
        log.info("app index: %d apps", len(apps))

    def ensure(self) -> None:
        if not self.apps or time.time() - self.built_at > 600:
            self.build()

    def find(self, query: str) -> tuple[str, str] | None:
        self.ensure()
        q = query.lower().strip()
        q = ALIASES.get(q, q)
        if q in self.apps:
            return q, self.apps[q]
        names = list(self.apps)
        hit = process.extractOne(q, names, scorer=fuzz.WRatio)
        if hit and hit[1] >= 80:
            return hit[0], self.apps[hit[0]]
        # prefix / contains fallback
        for n in names:
            if n.startswith(q) or q in n:
                return n, self.apps[n]
        return None


index = AppIndex()


def warm() -> None:
    threading.Thread(target=index.build, daemon=True).start()


def _launch(target: str) -> None:
    if IS_MAC and target.endswith(".app"):
        subprocess.Popen(["open", "-a", target])
    elif IS_LINUX and target.endswith(".desktop"):
        name = Path(target).stem
        if subprocess.run(["which", "gtk-launch"], capture_output=True).returncode == 0:
            subprocess.Popen(["gtk-launch", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            txt = Path(target).read_text("utf-8", errors="ignore")
            m = re.search(r"^Exec=(.+)$", txt, re.M)
            if not m:
                raise ToolError("Can't launch this app.")
            cmd = re.sub(r"%[a-zA-Z]", "", m.group(1)).strip()
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        open_path(target)


@tool(
    risk="low", tags=["open", "launch", "start", "app", "application", "program"],
    examples=["open_app(name='spotify')", "open_app(name='vs code')"],
)
def open_app(name: str) -> str:
    """Open / launch an installed application by name (fuzzy matched)."""
    hit = index.find(name)
    if not hit:
        raise ToolError(f"No installed app matches '{name}'.",
                        hint="Try list_installed_apps with a filter, or open_url for websites.")
    display, target = hit
    _launch(target)
    return f"Opened {display}."


@tool(risk="low", tags=["installed", "apps", "programs", "list"])
def list_installed_apps(filter: str = "") -> list[str]:
    """List installed applications, optionally filtered by a substring."""
    index.ensure()
    names = sorted(index.apps)
    if filter:
        f = filter.lower()
        names = [n for n in names if f in n] or [h[0] for h in process.extract(f, names, limit=15)]
    return names[:80]


def _procs_matching(name: str) -> list[psutil.Process]:
    q = ALIASES.get(name.lower().strip(), name.lower().strip())
    q_short = q.replace(" ", "")
    out = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            pname = (p.info["name"] or "").lower().removesuffix(".exe")
            if not pname:
                continue
            if pname == q_short or q_short in pname or fuzz.ratio(pname, q_short) >= 85 \
                    or (len(q.split()) > 1 and q.split()[-1] in pname):
                out.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


@tool(risk="medium", tags=["close", "quit", "exit", "kill", "app"],
      examples=["close_app(name='spotify')"])
def close_app(name: str, force: bool = False) -> str:
    """Close a running application (all its processes). Unsaved work may be lost."""
    procs = _procs_matching(name)
    protected = {"explorer", "system", "csrss", "winlogon", "python", "jarvis", "finder", "systemd"}
    procs = [p for p in procs if (p.info["name"] or "").lower().removesuffix(".exe") not in protected]
    if not procs:
        raise ToolError(f"'{name}' doesn't appear to be running.", hint="Use list_running_apps to check.")
    for p in procs:
        try:
            p.kill() if force else p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    gone, alive = psutil.wait_procs(procs, timeout=3)
    if alive and not force:
        return f"Asked {len(procs)} process(es) to close; {len(alive)} still running (use force=true)."
    return f"Closed {name} ({len(gone)} process(es))."


@tool(risk="low", tags=["running", "processes", "open apps", "list"])
def list_running_apps(top: int = 25) -> list[dict]:
    """List running apps with memory use, biggest first."""
    seen: dict[str, dict] = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            n = (p.info["name"] or "").removesuffix(".exe")
            mem = (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 2**20
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not n:
            continue
        e = seen.setdefault(n, {"name": n, "mem_mb": 0, "procs": 0})
        e["mem_mb"] += mem
        e["procs"] += 1
    rows = sorted(seen.values(), key=lambda r: -r["mem_mb"])[:top]
    for r in rows:
        r["mem_mb"] = round(r["mem_mb"])
    return rows


@tool(risk="low", tags=["focus", "switch", "bring", "window", "front"])
def focus_app(name: str) -> str:
    """Bring a running app's window to the front."""
    if IS_MAC:
        hit = index.find(name)
        app = Path(hit[1]).stem if hit else name
        subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'], timeout=5)
        return f"Focused {app}."
    if IS_LINUX:
        if subprocess.run(["which", "wmctrl"], capture_output=True).returncode == 0:
            r = subprocess.run(["wmctrl", "-a", name], capture_output=True)
            if r.returncode == 0:
                return f"Focused {name}."
        raise ToolError("Couldn't focus that window.", hint="Install wmctrl for window control on Linux.")
    # Windows: enumerate top-level windows by owning process
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pids = {p.pid for p in _procs_matching(name)}
    if not pids:
        raise ToolError(f"'{name}' isn't running.", hint="Use open_app to start it.")
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise ToolError(f"'{name}' has no visible window.")
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Alt-key trick so Windows allows SetForegroundWindow from a background process
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)
    return f"Focused {name}."
