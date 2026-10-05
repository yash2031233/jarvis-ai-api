"""Windows on this PC: list them, find the one the user means, and capture it even when it's behind other windows.

Finding a window ("chrome", "the claude app", "terminal", "Spotify", part of a title...): every visible top-level
window is scored on its app (process) name, friendly aliases and its title; the most recently used one wins ties.
Capturing: Windows' PrintWindow(PW_RENDERFULLCONTENT) asks the app to draw itself, so a covered window (Chrome,
Electron apps like Claude / Discord / VS Code, Office...) can be read without bringing it to the front. Minimized
windows are restored without taking focus, captured, and minimized again. Falls back to a plain screen grab.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

import numpy as np

# what people call an app -> process names (lowercase, no .exe)
ALIASES: dict[str, list[str]] = {
    "chrome": ["chrome"], "google chrome": ["chrome"], "browser": ["chrome", "msedge", "firefox", "brave", "opera"],
    "edge": ["msedge"], "firefox": ["firefox"], "terminal": ["windowsterminal", "cmd", "powershell", "pwsh", "wezterm-gui",
                                                            "alacritty", "conhost", "mintty", "bash"],
    "command prompt": ["cmd", "windowsterminal"], "powershell": ["powershell", "pwsh", "windowsterminal"],
    "file explorer": ["explorer"], "explorer": ["explorer"], "files": ["explorer"], "vs code": ["code"], "vscode": ["code"],
    "code": ["code"], "word": ["winword"], "excel": ["excel"], "powerpoint": ["powerpnt"], "outlook": ["outlook", "olk"],
    "claude": ["claude"], "spotify": ["spotify"], "discord": ["discord"], "steam": ["steamwebhelper", "steam"],
    "notepad": ["notepad"], "settings": ["systemsettings"], "task manager": ["taskmgr"], "teams": ["ms-teams", "teams"],
    "obsidian": ["obsidian"], "slack": ["slack"], "zoom": ["zoom"], "whatsapp": ["whatsapp"],
    "jarvis": ["pythonw", "python"],
}
FRIENDLY = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "windowsterminal": "Terminal", "explorer": "File Explorer",
            "code": "VS Code", "winword": "Word", "powerpnt": "PowerPoint", "systemsettings": "Settings",
            "steamwebhelper": "Steam", "taskmgr": "Task Manager", "applicationframehost": "app"}
SKIP_TITLES = {"program manager", "default ime", "msctfime ui", "windows input experience", "nvidia geforce overlay",
               "settings" if False else "\0"}


@dataclass
class Win:
    hwnd: int
    title: str
    process: str
    pid: int
    minimized: bool
    rect: tuple[int, int, int, int]      # left, top, width, height
    z: int                               # 0 = front-most

    @property
    def app(self) -> str:
        if self.process in ("pythonw", "python") and _norm(self.title) == "jarvis":
            return "Jarvis"
        return FRIENDLY.get(self.process, self.process.replace("_", " ").title())

    def info(self) -> dict[str, Any]:
        return {"app": self.app, "title": self.title, "minimized": self.minimized, "front": self.z == 0}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _user32():
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetWindow.restype = wintypes.HWND
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    return u


def list_windows() -> list[Win]:
    """Visible top-level app windows, front-most first."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    import psutil

    u = _user32()
    out: list[Win] = []
    names: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        ex = u.GetWindowLongW(hwnd, -20)                     # GWL_EXSTYLE
        if ex & 0x80:                                         # WS_EX_TOOLWINDOW: tooltips, overlays
            return True
        if u.GetWindow(hwnd, 4):                              # GW_OWNER: owned popups, not app windows
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title or title.lower() in SKIP_TITLES:
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        mini = bool(u.IsIconic(hwnd))
        if not mini and (r.right - r.left < 80 or r.bottom - r.top < 60):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            try:
                names[pid.value] = re.sub(r"\.exe$", "", psutil.Process(pid.value).name(), flags=re.I).lower()
            except Exception:
                names[pid.value] = "?"
        out.append(Win(int(hwnd), title, names[pid.value], pid.value, mini,
                       (r.left, r.top, r.right - r.left, r.bottom - r.top), len(out)))
        return True

    u.EnumWindows(cb, 0)
    return out


def find(query: str) -> Win | None:
    """The window the user means by `query` (app name, alias or part of the title)."""
    q = (query or "").strip().lower()
    q = re.sub(r"^(the |my )", "", q)
    q = re.sub(r"\s+(app|window|tab)$", "", q)
    if not q:
        return None
    wins = list_windows()
    procs = set(ALIASES.get(q, []))
    best, best_score = None, 0.0
    for w in wins:
        t = w.title.lower()
        s = 0.0
        if q == "jarvis" and w.app != "Jarvis":
            continue
        if w.process in procs:
            s = 90
        elif w.process == q.replace(" ", "") or q == w.app.lower():
            s = 88
        elif q in t or (_norm(q) and _norm(q) in _norm(t)):
            s = 80 + min(10, 10 * len(q) / max(1, len(t)))
        elif q in w.app.lower() or q in w.process:
            s = 70
        else:
            from rapidfuzz import fuzz

            f = max(fuzz.partial_ratio(q, t), fuzz.ratio(q, w.app.lower()))
            s = f * 0.6 if f >= 80 else 0
        if s:
            s += max(0, 5 - w.z) - (3 if w.minimized else 0)   # recently used and visible first
        if s > best_score:
            best, best_score = w, s
    return best


def capture(w: Win) -> np.ndarray | None:
    """The window's own picture (even when covered). None if the app can't draw itself that way."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    u = _user32()
    g = ctypes.windll.gdi32
    for fn, args, res in ((u.GetWindowDC, [wintypes.HWND], ctypes.c_void_p), (g.CreateCompatibleDC, [ctypes.c_void_p], ctypes.c_void_p),
                          (g.CreateCompatibleBitmap, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
                          (g.SelectObject, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
                          (u.PrintWindow, [wintypes.HWND, ctypes.c_void_p, ctypes.c_uint], wintypes.BOOL),
                          (g.GetDIBits, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
                          (g.DeleteObject, [ctypes.c_void_p], wintypes.BOOL), (g.DeleteDC, [ctypes.c_void_p], wintypes.BOOL),
                          (u.ReleaseDC, [wintypes.HWND, ctypes.c_void_p], ctypes.c_int)):
        fn.argtypes, fn.restype = args, res
    restored = False
    if w.minimized:
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow(w.hwnd, 4)                               # SW_SHOWNOACTIVATE: back on screen without focus
        restored = True
        import time

        time.sleep(0.35)
        r = wintypes.RECT()
        u.GetWindowRect(w.hwnd, ctypes.byref(r))
        w.rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    _, _, width, height = w.rect
    try:
        if width <= 0 or height <= 0:
            return None
        hdc_w = u.GetWindowDC(w.hwnd)
        hdc = g.CreateCompatibleDC(hdc_w)
        bmp = g.CreateCompatibleBitmap(hdc_w, width, height)
        g.SelectObject(hdc, bmp)
        ok = u.PrintWindow(w.hwnd, hdc, 2)                    # PW_RENDERFULLCONTENT

        class BMI(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]
        bmi = BMI(40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        lines = g.GetDIBits(hdc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
        g.DeleteObject(bmp)
        g.DeleteDC(hdc)
        u.ReleaseDC(w.hwnd, hdc_w)
        if not ok or lines != height:
            return None
        img = np.frombuffer(buf.raw, np.uint8).reshape(height, width, 4)[:, :, :3].copy()
        if float(img.mean()) < 2.0:                           # drew nothing (some GPU-only apps): no picture
            return None
        return img
    finally:
        if restored:
            u.ShowWindow(w.hwnd, 7)                           # SW_SHOWMINNOACTIVE: back where it was
