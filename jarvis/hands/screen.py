"""Screen + input: read what's on screen with OCR (no vision model, ~1 s), find and click text,
type, press keys, scroll, screenshot - and `look_at_screen` when a picture really needs a vision model.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import numpy as np

from .. import config
from ..vision import ocr
from .registry import ToolError, tool

SHOT_DIR = config.DATA_DIR / "screenshots"


def _dpi_aware() -> None:
    """So screenshot pixels and mouse coordinates agree on scaled (125%/150%) displays."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


_dpi_aware()


def _window_rect(title: str) -> tuple[int, int, int, int] | None:
    """(left, top, width, height) of the first visible window whose title contains `title`."""
    if not title:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            n = u.GetWindowTextLengthW(hwnd)
            if n and u.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if title.lower() in buf.value.lower():
                    found.append(hwnd)
                    return False
            return True

        u.EnumWindows(cb, 0)
        if not found:
            return None
        r = wintypes.RECT()
        u.GetWindowRect(found[0], ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return None


def grab(window: str = "") -> tuple[np.ndarray, int, int]:
    """Screenshot (BGR) of the whole desktop or one window, plus its top-left in screen coordinates."""
    try:
        import mss
    except ImportError:
        raise ToolError("Screen capture needs mss: pip install mss")
    with getattr(mss, "MSS", None)() if hasattr(mss, "MSS") else mss.mss() as m:
        if window:
            rect = _window_rect(window)
            if rect is None:
                raise ToolError(f"No open window matching '{window}'.", hint="Use list_running_apps or omit window.")
            left, top, w, h = rect
            mon = {"left": left, "top": top, "width": max(1, w), "height": max(1, h)}
        else:
            mon = m.monitors[0]  # all monitors
        shot = m.grab(mon)
    img = np.asarray(shot)[:, :, :3].copy()  # BGRA -> BGR
    return img, int(mon["left"]), int(mon["top"])


def _lines(window: str = "") -> tuple[list[dict[str, Any]], int, int]:
    img, ox, oy = grab(window)
    try:
        return ocr.read_image(img), ox, oy
    except ocr.OCRUnavailable as e:
        raise ToolError(str(e), hint="Use look_at_screen (vision model) instead.")


@tool(risk="low", tags=["screen", "read", "what's on my screen", "ocr", "text", "window"],
      examples=["read_screen()", "read_screen(window='Chrome')"])
def read_screen(window: str = "", max_chars: int = 6000) -> dict:
    """Read all text on the screen (or in one window by title) with OCR in about a second - no vision model.
    Use this FIRST for 'what's on my screen', reading an error, a page, a dialog."""
    t0 = time.time()
    lines, _, _ = _lines(window)
    text = ocr.text_of(lines)
    return {"text": text[:max_chars], "truncated": len(text) > max_chars, "lines": len(lines),
            "ms": int((time.time() - t0) * 1000)}


@tool(risk="low", tags=["find", "where", "screen", "button", "text", "locate"])
def find_on_screen(text: str, window: str = "") -> dict:
    """Find where a piece of text is on screen (OCR) - returns its centre in screen coordinates, best match first."""
    lines, ox, oy = _lines(window)
    hits = ocr.find(lines, text)
    if not hits:
        raise ToolError(f"'{text}' isn't visible on screen.", hint="read_screen to see what is there.")
    return {"matches": [{"text": h["text"], "x": h["x"] + ox, "y": h["y"] + oy, "score": h["score"]} for h in hits[:5]]}


def _mouse():
    try:
        from pynput.mouse import Button, Controller
    except ImportError:
        raise ToolError("Mouse control needs pynput: pip install pynput")
    return Controller(), Button


@tool(risk="medium", tags=["click", "press", "button", "link", "menu", "screen", "tap"],
      examples=["click_text(text='Sign in')", "click_text(text='Save', window='Notepad')"])
def click_text(text: str, window: str = "", button: str = "left", double: bool = False) -> dict:
    """Find text on screen with OCR and click it (a button label, menu item, link, list row).
    Much faster than a vision round-trip. button: left|right."""
    lines, ox, oy = _lines(window)
    hits = ocr.find(lines, text)
    if not hits:
        raise ToolError(f"'{text}' isn't visible on screen.", hint="read_screen first, or scroll / open the window.")
    h = hits[0]
    x, y = h["x"] + ox, h["y"] + oy
    mouse, Button = _mouse()
    mouse.position = (x, y)
    time.sleep(0.05)
    mouse.click(Button.right if button == "right" else Button.left, 2 if double else 1)
    return {"clicked": h["text"], "x": x, "y": y}


@tool(risk="medium", tags=["click", "coordinates", "mouse"])
def click_at(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click screen coordinates (from find_on_screen)."""
    mouse, Button = _mouse()
    mouse.position = (x, y)
    time.sleep(0.03)
    mouse.click(Button.right if button == "right" else Button.left, 2 if double else 1)
    return f"Clicked at {x},{y}."


@tool(risk="medium", tags=["type", "write", "enter text", "keyboard", "fill"],
      examples=["type_text(text='hello world', press_enter=true)"])
def type_text(text: str, press_enter: bool = False, window: str = "") -> str:
    """Type text into the focused window (optionally focus a window by title first)."""
    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        raise ToolError("Typing needs pynput: pip install pynput")
    if window:
        from .apps import focus_app

        focus_app(window)
        time.sleep(0.25)
    kb = Controller()
    kb.type(text)
    if press_enter:
        kb.press(Key.enter)
        kb.release(Key.enter)
    return f"Typed {len(text)} characters."


_KEYS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd", "cmd": "cmd",
         "super": "cmd", "enter": "enter", "return": "enter", "esc": "esc", "escape": "esc", "tab": "tab",
         "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete", "up": "up",
         "down": "down", "left": "left", "right": "right", "home": "home", "end": "end", "pageup": "page_up",
         "pagedown": "page_down"}


@tool(risk="medium", tags=["press", "key", "shortcut", "hotkey", "keyboard"],
      examples=["press_keys(keys='ctrl+s')", "press_keys(keys='alt+tab')", "press_keys(keys='enter', times=2)"])
def press_keys(keys: str, times: int = 1) -> str:
    """Press a key or shortcut like 'ctrl+s', 'alt+tab', 'win+d', 'f5', 'enter'."""
    try:
        from pynput.keyboard import Controller, Key, KeyCode
    except ImportError:
        raise ToolError("Keys need pynput: pip install pynput")
    kb = Controller()
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    seq = []
    for p in parts:
        name = _KEYS.get(p, p)
        if hasattr(Key, name):
            seq.append(getattr(Key, name))
        elif len(p) == 1:
            seq.append(KeyCode.from_char(p))
        else:
            raise ToolError(f"Unknown key '{p}'.", hint="e.g. ctrl, alt, shift, win, enter, tab, f5, a")
    for _ in range(max(1, min(times, 50))):
        for k in seq:
            kb.press(k)
        for k in reversed(seq):
            kb.release(k)
        time.sleep(0.03)
    return f"Pressed {keys}" + (f" x{times}" if times > 1 else "")


@tool(risk="low", tags=["scroll", "page", "down", "up", "wheel"])
def scroll(amount: int = -5) -> str:
    """Scroll the mouse wheel where the pointer is. Positive = up, negative = down."""
    mouse, _ = _mouse()
    mouse.scroll(0, amount)
    return f"Scrolled {'up' if amount > 0 else 'down'} {abs(amount)}."


@tool(risk="low", tags=["screenshot", "capture", "screen", "picture", "send"])
def screenshot(window: str = "") -> dict:
    """Save a screenshot (whole screen or a window) and return the file path."""
    import cv2

    img, _, _ = grab(window)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = SHOT_DIR / f"screen_{time.strftime('%Y%m%d-%H%M%S')}.png"
    cv2.imwrite(str(p), img)
    return {"path": str(p), "size": [int(img.shape[1]), int(img.shape[0])]}


@tool(risk="low", timeout=120, tags=["look", "screen", "see", "picture", "chart", "image", "what does it look like"])
async def look_at_screen(question: str = "", window: str = "") -> dict:
    """Look at the screen with the vision model - for pictures, charts and layout that text can't capture.
    For text, read_screen is much faster."""
    import asyncio

    from ..vision import see

    img, _, _ = await asyncio.to_thread(grab, window)
    try:
        ans = await see.ask([img], question or "Describe what's on the screen.", max_side=1600)
    except see.NoVision as e:
        raise ToolError(str(e), hint="Use read_screen for text.")
    return {"answer": ans}
