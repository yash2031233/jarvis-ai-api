"""Context awareness: what the user is looking at, so "summarize this" just works."""

from __future__ import annotations

import platform
import re
import subprocess
from datetime import datetime

REFERS_TO_CONTEXT = re.compile(r"\b(this|that|it|these|those|clipboard|copied|selected|selection|here)\b", re.I)


def active_window() -> str:
    try:
        sysname = platform.system()
        if sysname == "Windows":
            import ctypes

            u = ctypes.windll.user32
            hwnd = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value
        if sysname == "Darwin":
            r = subprocess.run(["osascript", "-e",
                                'tell application "System Events" to get name of first process whose frontmost is true'],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except Exception:
        return ""


def clipboard_preview(limit: int = 1500) -> str:
    try:
        import pyperclip

        t = pyperclip.paste() or ""
    except Exception:
        return ""
    t = t.strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def build(query: str) -> str:
    lines = [f"Now: {datetime.now().strftime('%A %Y-%m-%d %I:%M %p')}", f"OS: {platform.system()} {platform.release()}"]
    win = active_window()
    if win and "jarvis" not in win.lower():
        lines.append(f"Active window: {win}")
    # Only share the clipboard with the model when the user is clearly referring to it,
    # so passwords etc. that happen to be copied aren't sent to the API on every request.
    if REFERS_TO_CONTEXT.search(query):
        clip = clipboard_preview()
        if clip:
            lines.append(f"Clipboard (may be what 'this' refers to):\n<<<\n{clip}\n>>>")
    return "\n".join(lines)
