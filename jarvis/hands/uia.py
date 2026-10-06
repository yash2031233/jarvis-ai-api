"""Click anything by name - no vision, no OCR. Windows UI Automation (the accessibility layer screen readers use)
lists every button, link, image, menu item and text box of an app or web page (Chrome, Edge, Firefox, Electron apps,
Office, Settings...) with its name and exact on-screen box - including what's further down the page. So "click Add to
cart" = find that element in the list, scroll it into view, read its fresh position, click the middle of it.
Right-click and double-click work the same way (right-click an image -> "Save image as..." is then a menu item to
click by name). Windows only; elsewhere the screen tools (click_text / point_at) still work.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from .registry import ToolError, tool

log = logging.getLogger(__name__)

CLICKABLE = {"Button", "Hyperlink", "MenuItem", "TabItem", "CheckBox", "RadioButton", "Edit", "ComboBox", "Image",
             "ListItem", "TreeItem", "SplitButton", "MenuBar", "Document", "DataItem", "Thumb", "Slider", "Spinner",
             "Text", "Group", "Custom", "Pane", "Window"}
ACTIONABLE = {"Button", "Hyperlink", "MenuItem", "TabItem", "CheckBox", "RadioButton", "Edit", "ComboBox", "Image",
              "ListItem", "TreeItem", "SplitButton", "DataItem", "Slider", "Spinner"}
ROLE_WORDS = {"button": "Button", "link": "Hyperlink", "image": "Image", "picture": "Image", "photo": "Image",
              "icon": "Button", "tab": "TabItem", "checkbox": "CheckBox", "check box": "CheckBox", "box": "Edit",
              "field": "Edit", "input": "Edit", "search": "Edit", "menu": "MenuItem", "option": "ListItem",
              "radio": "RadioButton", "dropdown": "ComboBox", "drop-down": "ComboBox"}
MAX_NODES, MAX_SECONDS = 8000, 6.0


@dataclass
class El:
    id: int
    role: str
    name: str
    rect: tuple[int, int, int, int]       # left, top, width, height (screen px)
    offscreen: bool
    value: str
    ctl: Any


_cache: dict[str, Any] = {"window": None, "els": [], "t": 0.0}
_lock = threading.Lock()


def _auto():
    if sys.platform != "win32":
        raise ToolError("Clicking by name uses Windows UI Automation (Windows only).",
                        hint="Use click_text, or point_at to click something by sight.")
    try:
        import uiautomation as auto
    except ImportError:
        raise ToolError("Clicking by name needs the uiautomation package.", hint="pip install uiautomation")
    return auto


def _root(window: str):
    """The window to search: by name ('chrome', 'spotify', 'settings'), else the one in front."""
    auto = _auto()
    from ..vision import windows

    if window:
        hits = [w for w in windows.list_windows() if not w.minimized and w.rect[2] > 50 and w.rect[3] > 50]
        best = windows.find(window)
        same = [w for w in hits if best and w.process == best.process]
        w = best
        if best is not None and (best.minimized or best.rect[2] * best.rect[3] < 300 * 300) and same:
            w = max(same, key=lambda w: w.rect[2] * w.rect[3])     # matched a popup / tray window: the app's real one
        if w is None:
            raise ToolError(f"No open window matching '{window}'.", hint="list_windows shows what's open.")
        return auto.ControlFromHandle(w.hwnd), w
    return auto.GetForegroundControl(), None


def _walk(root) -> list[El]:
    auto = _auto()
    out: list[El] = []
    t0, n = time.time(), 0
    for c, _depth in auto.WalkControl(root, maxDepth=90):
        n += 1
        if n > MAX_NODES or time.time() - t0 > MAX_SECONDS:
            break
        try:
            role = c.ControlTypeName.removesuffix("Control")
            if role not in CLICKABLE:
                continue
            name = (c.Name or "").strip()
            value = ""
            if role in ("Edit", "ComboBox"):
                try:
                    value = (c.GetValuePattern().Value or "")[:80]
                except Exception:
                    pass
            if not name and role not in ("Edit", "ComboBox", "Image"):
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            out.append(El(len(out) + 1, role, re.sub(r"\s+", " ", name)[:120], (r.left, r.top, r.width(), r.height()),
                          bool(c.IsOffscreen), value, c))
        except Exception:
            continue
    return out


def scan(window: str = "") -> tuple[list[El], Any]:
    """Every named control in the window. Chrome builds its page tree the first time anyone asks, so a suspiciously
    small first look is taken again a moment later."""
    _auto()
    with _lock:
        root, w = _root(window)
        els = _walk(root)
        if len(els) < 80 and (w is None or any(b in w.process.lower() for b in ("chrome", "msedge", "brave", "firefox", "opera"))):
            time.sleep(1.0)
            more = _walk(root)
            if len(more) > len(els):
                els = more
        _cache.update(window=window, els=els, t=time.time())
        return els, w


def score(el: El, query: str) -> float:
    """How well an element matches 'the Add to cart button' / 'search box' / 'Sign in'."""
    q = query.lower().strip()
    want_role = None
    for word, role in ROLE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", q):
            want_role = role
            q = re.sub(rf"\b(the|a|an)\b|\b{re.escape(word)}\b", " ", q)
    q = re.sub(r"\b(the|a|an|click|on|named|called|labelled|labeled)\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" '\"")
    name = el.name.lower()
    if not q:
        s = 50.0 if want_role else 0.0
    elif name == q:
        s = 100.0
    elif name.startswith(q) or q.startswith(name) and len(name) > 3:
        s = 90.0
    elif q in name:
        s = 82.0
    else:
        s = 0.8 * max(fuzz.token_set_ratio(q, name), fuzz.partial_ratio(q, name) if len(q) > 3 else 0)
    if el.role == "Edit" and el.value and q and q in el.value.lower():
        s = max(s, 80.0)
    if want_role and el.role == want_role:
        s += 12
    elif want_role == "Edit" and el.role == "ComboBox":
        s += 10
    if el.role in ACTIONABLE:
        s += 4
    if not el.offscreen:
        s += 2
    return s


def best(els: list[El], query: str, nth: int = 1) -> tuple[El | None, list[El]]:
    ranked = sorted(els, key=lambda e: -score(e, query))
    good = [e for e in ranked if score(e, query) >= 62]
    if len(good) >= nth:
        return good[nth - 1], good[:6]
    return None, ranked[:6]


def _visible_rect(el: El, w) -> tuple[int, int, int, int]:
    r = el.ctl.BoundingRectangle
    return r.left, r.top, r.width(), r.height()


def _scroll_into_view(el: El, w) -> None:
    """Bring an element that's further down (or up) the page onto the screen."""
    try:
        el.ctl.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.35)
    except Exception:
        pass
    if not el.ctl.IsOffscreen:
        return
    # no scroll pattern (most web content): wheel over the window until it's on screen
    from .screen import _mouse

    mouse, _ = _mouse()
    if w is not None:
        wx, wy, ww, wh = w.rect
        mouse.position = (wx + ww // 2, wy + wh // 2)
    for _ in range(40):
        top = el.ctl.BoundingRectangle.top
        if not el.ctl.IsOffscreen:
            break
        screen_mid = (w.rect[1] + w.rect[3] // 2) if w is not None else 700
        mouse.scroll(0, -3 if top > screen_mid else 3)
        time.sleep(0.12)
    time.sleep(0.3)


def _describe(e: El) -> str:
    extra = f" = '{e.value}'" if e.value else ""
    return f"[{e.id}] {e.role}: {e.name or '(no name)'}{extra}" + ("  (below/above - scrolls into view)" if e.offscreen else "")


@tool(risk="low", timeout=30, tags=["buttons", "links", "elements", "what can i click", "page", "ui", "controls"],
      examples=["ui_elements(window='chrome', find='cart')", "ui_elements(window='spotify')"])
def ui_elements(window: str = "", find: str = "", kind: str = "", limit: int = 60) -> str:
    """List the buttons, links, images, menu items and text boxes of an app or web page by name - the WHOLE page,
    also what's further down - so you know exactly what you can click with ui_click. `find` narrows it to names like
    that; `kind` to button / link / image / edit / menuitem / tab / checkbox. `window` = app ('chrome', 'spotify'...;
    default: the one in front)."""
    els, w = scan(window)
    if kind:
        k = ROLE_WORDS.get(kind.lower().rstrip("s"), kind.title().replace(" ", ""))
        els = [e for e in els if e.role.lower() == k.lower()]
    if find:
        els = sorted([e for e in els if score(e, find) >= 55], key=lambda e: -score(e, find))
    else:
        els = [e for e in els if e.role in ACTIONABLE]
    head = f"{len(els)} elements in {w.title[:60] if w else 'the front window'}"
    lines = [_describe(e) for e in els[:max(1, min(limit, 200))]]
    return head + ("\n" + "\n".join(lines) if lines else "\n(nothing like that - try another word, or no `find`)")


@tool(risk="medium", timeout=45, tags=["click", "press", "button", "link", "tap", "open", "right click", "select", "check"],
      examples=["ui_click(target='Add to cart', window='chrome')", "ui_click(target='Sign in button')",
                "ui_click(target='the cat picture', window='chrome', button='right')"])
def ui_click(target: str, window: str = "", button: str = "left", double: bool = False, nth: int = 1) -> dict:
    """Click a button / link / image / menu item / tab / checkbox BY ITS NAME in any app or web page - exact, no
    vision needed. Finds it anywhere on the page (scrolls down to it if needed) and clicks its middle. button='right'
    for a context menu (then ui_click the menu item by name). `nth`=2 for the second match. Use this FIRST for
    clicking; click_text / point_at are for things with no accessible name."""
    from .screen import _mouse, focus

    els, w = scan(window)
    el, close = best(els, target, max(1, int(nth)))
    if el is None:
        raise ToolError(f"Nothing called '{target}' here.",
                        hint="Closest: " + "; ".join(_describe(e) for e in close[:5]) + ". Or ui_elements to see all.")
    if window:
        focus(window)
        time.sleep(0.15)
    if el.offscreen:
        _scroll_into_view(el, w)
    x, y, ww, hh = _visible_rect(el, w)
    if ww <= 0 or hh <= 0:
        raise ToolError(f"'{el.name}' is there but not visible right now.", hint="It may be in a closed menu or tab.")
    cx, cy = x + ww // 2, y + hh // 2
    if w is not None and not _on_top(cx, cy, w.hwnd):
        # another window covers that spot (Windows doesn't always let us bring this one to the front): a mouse click
        # would land in the wrong app, so press it through accessibility instead - or stop
        if button != "right" and not double and _invoke(el):
            return {"clicked": _describe(el), "how": "invoked (the window was covered, no mouse needed)"}
        raise ToolError(f"Another window is covering '{el.name}', so clicking there would hit the wrong app.",
                        hint=f"Bring {w.title[:40]!r} to the front (focus_window) and try again.")
    mouse, Button = _mouse()
    mouse.position = (cx, cy)
    time.sleep(0.05)
    mouse.click(Button.right if button == "right" else Button.left, 2 if double else 1)
    return {"clicked": _describe(el), "at": [cx, cy], "button": button}


def _on_top(x: int, y: int, hwnd: int) -> bool:
    """Is this window the one actually under that screen point?"""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.WindowFromPoint.restype = wintypes.HWND
    u.GetAncestor.restype = wintypes.HWND
    at = u.WindowFromPoint(wintypes.POINT(x, y))
    return bool(at) and (u.GetAncestor(at, 2) or at) == hwnd          # GA_ROOT


def _invoke(el: El) -> bool:
    """Press a control without the mouse (Invoke / Toggle / Select / the default action)."""
    c = el.ctl
    for get, act in (("GetInvokePattern", "Invoke"), ("GetTogglePattern", "Toggle"),
                     ("GetSelectionItemPattern", "Select"), ("GetLegacyIAccessiblePattern", "DoDefaultAction")):
        try:
            pat = getattr(c, get)()
            if pat:
                getattr(pat, act)()
                return True
        except Exception:
            continue
    return False


@tool(risk="medium", timeout=45, tags=["type", "fill", "field", "search box", "input", "form", "enter"],
      examples=["ui_fill(field='Search', text='rtx 5090', window='chrome', press_enter=true)"])
def ui_fill(field: str, text: str, window: str = "", press_enter: bool = False) -> dict:
    """Type into a text box found BY ITS NAME (search box, 'Email', 'Message'...) in any app or web page: finds it
    (scrolling to it if needed), clicks into it, replaces what's there with `text`."""
    from .screen import _mouse

    els, w = scan(window)
    edits = [e for e in els if e.role in ("Edit", "ComboBox", "Document")]
    el, close = best(edits or els, field + " box")
    if el is None:
        raise ToolError(f"No text box called '{field}' here.", hint="Text boxes: " + "; ".join(_describe(e) for e in edits[:8]))
    if el.offscreen:
        _scroll_into_view(el, w)
    set_ok = False
    try:
        el.ctl.SetFocus()
    except Exception:
        pass
    try:                                     # straight into the box: no keystrokes that could land in another app
        el.ctl.GetValuePattern().SetValue(text)
        set_ok = True
    except Exception:
        pass
    x, y, ww, hh = _visible_rect(el, w)
    front = w is None or _on_top(x + ww // 2, y + hh // 2, w.hwnd)
    if not set_ok:
        if not front:
            raise ToolError(f"Another window is covering '{el.name or field}'.", hint="Bring that window to the front first.")
        from .screen import type_text

        mouse, Button = _mouse()
        mouse.position = (x + ww // 2, y + hh // 2)
        mouse.click(Button.left, 1)
        time.sleep(0.1)
        type_text(text)
    if press_enter:
        if not front:
            raise ToolError(f"Typed '{text}', but couldn't press Enter: another window is in front.",
                            hint="Bring that window to the front, or ui_click the search / submit button.")
        from pynput.keyboard import Controller, Key

        Controller().tap(Key.enter)
    return {"filled": _describe(el), "text": text, "entered": bool(press_enter)}
