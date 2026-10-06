"""Click anything by name - no vision, no OCR. Windows UI Automation (the accessibility layer screen readers use)
lists every button, link, image, menu item and text box of an app or web page (Chrome, Edge, Firefox, Electron apps,
Office, Settings...) with its name and exact on-screen box - including what's further down the page. So "click Add to
cart" = find that element in the list, scroll it into view, read its fresh position, click the middle of it.
Right-click and double-click work the same way (right-click an image -> "Save image as..." is then a menu item to
click by name). Windows only; elsewhere the screen tools (click_text / point_at) still work.

Badly built pages: the name Windows gives a control is what a screen reader would say - its text, aria-label, alt,
title or <label>. Icon-only buttons often have none, so every control also gets backup names, best first: its
tooltip, the text label right beside / above it ("Quantity" -> the + next to it), its HTML id / class made readable,
and - only for icons with nothing else - a few words from the vision model ("trash can", "gear"). The position still
comes from Windows, so the click stays exact even when the name came from a picture.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
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
              "radio": "RadioButton", "dropdown": "ComboBox", "drop-down": "ComboBox", "switch": "Button",
              "toggle": "Button"}
NEXT_TO = re.compile(r"\b(next to|beside|by the|near|for the|left of|right of)\b|[+×✕⚙…⋮]", re.I)
MAX_NODES, MAX_SECONDS = 8000, 6.0
PLACEHOLDER = re.compile(r"^to get missing image descriptions|^unlabeled graphic|^image$|^graphic$", re.I)
GENERIC_CLS = re.compile(r"^(icon|btn|button|svg|img|image|item|wrapper|container|inner|outer|root|div|span|"
                         r"[a-z]{1,3}-?\d*|[a-z0-9_-]*\d[a-z0-9_-]*\d[a-z0-9_-]*|css-[\w-]+|sc-[\w-]+)$", re.I)


@dataclass
class El:
    id: int
    role: str
    name: str
    rect: tuple[int, int, int, int]       # left, top, width, height (screen px)
    offscreen: bool
    value: str
    ctl: Any
    help: str = ""                        # tooltip (title=...)
    aid: str = ""                         # HTML id / the app's automation id
    cls: str = ""
    aliases: list = field(default_factory=list)   # backup names: [(text, weight, where it came from)]

    def names(self) -> list[tuple[str, float, str]]:
        return ([(self.name, 1.0, "")] if self.name else []) + self.aliases


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


@contextlib.contextmanager
def _ui_thread():
    """UI Automation is COM: each thread that uses it has to set it up (tools run in worker threads)."""
    auto = _auto()
    init = None
    with contextlib.suppress(Exception):
        init = auto.UIAutomationInitializerInThread()
    try:
        yield auto
    finally:
        del init


def _readable(token: str) -> str:
    """'qty-plus' / 'addToCart' / 'btn_close' -> 'qty plus' / 'add to cart' / 'close'; generated junk -> ''."""
    parts = []
    for t in (token or "").split():
        if GENERIC_CLS.match(t):
            continue
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        t = re.sub(r"[-_.:]+", " ", t).lower()
        t = re.sub(r"\b(btn|button|icon|ico|svg|img|js|ui|el|wrapper|container)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 1 and not re.fullmatch(r"[\d ]+", t):
            parts.append(t)
    return " ".join(dict.fromkeys(parts))[:60]


def _root(window: str):
    """The window to search: by name ('chrome', 'spotify', 'settings'), else the one in front."""
    auto = _auto()
    from ..vision import windows

    if window:
        hits = [w for w in windows.list_windows() if not w.minimized and w.rect[2] > 50 and w.rect[3] > 50]
        best_w = windows.find(window)
        same = [w for w in hits if best_w and w.process == best_w.process]
        w = best_w
        if best_w is not None and (best_w.minimized or best_w.rect[2] * best_w.rect[3] < 300 * 300) and same:
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
            if PLACEHOLDER.match(name):
                name = ""
            value = ""
            if role in ("Edit", "ComboBox"):
                with contextlib.suppress(Exception):
                    value = (c.GetValuePattern().Value or "")[:80]
            if not name and role not in ACTIONABLE and role != "Group":
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            aid = help_ = cls = ""
            with contextlib.suppress(Exception):
                aid, help_, cls = (c.AutomationId or ""), (c.HelpText or ""), (c.ClassName or "")
            if role == "Group" and not (aid or name):                     # plain layout boxes
                continue
            out.append(El(len(out) + 1, role, re.sub(r"\s+", " ", name)[:120], (r.left, r.top, r.width(), r.height()),
                          bool(c.IsOffscreen), value, c, help_.strip()[:80], aid.strip()[:60], cls.strip()[:60]))
        except Exception:
            continue
    return out


def scan(window: str = "") -> tuple[list[El], Any]:
    """Every control in the window. Chrome builds its page tree the first time anyone asks, so a suspiciously small
    first look is taken again a moment later."""
    _auto()
    with _lock:
        root, w = _root(window)
        els = _walk(root)
        if len(els) < 80 and (w is None or any(b in w.process.lower() for b in ("chrome", "msedge", "brave", "firefox", "opera"))):
            time.sleep(1.0)
            more = _walk(root)
            if len(more) > len(els):
                els = more
        backup_names(els)
        _cache.update(window=window, els=els, t=time.time())
        return els, w


def backup_names(els: list[El]) -> None:
    """Tooltip, the label beside / above it, and the id / class - for controls whose own name is missing or a symbol."""
    labels = [e for e in els if e.name and e.role in ("Text", "Group", "Custom") and len(e.name) < 60]
    for e in els:
        weak = not e.name or len(re.sub(r"[\W_]", "", e.name)) < 2          # '', '⚙', '+', '×'
        if e.help and e.help != e.name:
            e.aliases.append((e.help, 0.97, "tooltip"))
        if weak and e.role in ACTIONABLE:
            lab = _label_for(e, labels)
            if lab:
                e.aliases.append((lab, 0.88, "label beside it"))
        if weak or e.role == "Group":
            for raw, why in ((e.aid, "id"), (e.cls, "class")):
                r = _readable(raw)
                if r and r != e.name.lower():
                    e.aliases.append((r, 0.84, why))


def _label_for(e: El, labels: list[El]) -> str:
    """The text a person would read as this control's label: just left of it on the same line, else right above."""
    x, y, w, h = e.rect
    cy = y + h / 2
    best_d, best_t = 1e9, ""
    for t in labels:
        tx, ty, tw, th = t.rect
        if t is e or (tx <= x and ty <= y and tx + tw >= x + w and ty + th >= y + h):
            continue                                                         # itself / a box around it
        if abs((ty + th / 2) - cy) < max(h, th) * 0.6 and tx + tw <= x + 4:
            d = x - (tx + tw)                                                # same line, to the left
        elif ty + th <= y + 4 and tx < x + w and tx + tw > x - 20:
            d = (y - (ty + th)) * 1.5 + 10                                   # above it
        else:
            continue
        if 0 <= d < 260 and d < best_d:
            best_d, best_t = d, t.name
    return best_t


def _query(query: str) -> tuple[str, str | None]:
    q = query.lower().strip()
    want_role = None
    for word, role in ROLE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", q):
            want_role = role
            q = re.sub(rf"\b{re.escape(word)}\b", " ", q)
    q = re.sub(r"\b(the|a|an|click|on|named|called|labelled|labeled|next to|beside|by|near|for|that says|with)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip(" '\""), want_role


def _match(name: str, q: str) -> float:
    name = name.lower()
    if not q or not name:
        return 0.0
    if name == q:
        return 100.0
    if name.startswith(q) or (q.startswith(name) and len(name) > 3):
        return 90.0
    if q in name:
        return 82.0
    return 0.8 * max(fuzz.token_set_ratio(q, name), fuzz.partial_ratio(q, name) if len(q) > 3 else 0)


def score(el: El, query: str) -> float:
    """How well an element matches 'the Add to cart button' / 'search box' / 'the + next to Quantity'."""
    q, want_role = _query(query)
    q = re.sub(r"^[+×✕⚙…⋮]\s*", "", q).strip()
    if not q and want_role:
        s = 50.0
    else:
        s = max((_match(n, q) * wgt for n, wgt, _ in el.names()), default=0.0)
        if el.role == "Edit" and el.value and q and q in el.value.lower():
            s = max(s, 80.0)
        if s < 40:
            return s
    if want_role and el.role == want_role:
        s += 12
    elif want_role == "Edit" and el.role == "ComboBox":
        s += 10
    if el.role in ACTIONABLE:
        s += 4
    elif want_role or NEXT_TO.search(query):
        s -= 12                    # "the switch next to Notifications": the control, not the word "Notifications"
    if not el.offscreen:
        s += 2
    return s


def best(els: list[El], query: str, nth: int = 1) -> tuple[El | None, list[El]]:
    ranked = sorted(els, key=lambda e: -score(e, query))
    good = [e for e in ranked if score(e, query) >= 62]
    if len(good) >= nth:
        return good[nth - 1], good[:6]
    return None, ranked[:6]


def _visible_rect(el: El) -> tuple[int, int, int, int]:
    r = el.ctl.BoundingRectangle
    return r.left, r.top, r.width(), r.height()


def _scroll_into_view(el: El, w) -> None:
    """Bring an element that's further down (or up) the page onto the screen."""
    with contextlib.suppress(Exception):
        el.ctl.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.35)
    if not el.ctl.IsOffscreen:
        return
    # no scroll pattern (most web content): wheel over the window until it's on screen
    from .screen import _mouse

    mouse, _ = _mouse()
    if w is not None:
        wx, wy, ww, wh = w.rect
        mouse.position = (wx + ww // 2, wy + wh // 2)
    for _ in range(40):
        if not el.ctl.IsOffscreen:
            break
        top = el.ctl.BoundingRectangle.top
        screen_mid = (w.rect[1] + w.rect[3] // 2) if w is not None else 700
        mouse.scroll(0, -3 if top > screen_mid else 3)
        time.sleep(0.12)
    time.sleep(0.3)


def _describe(e: El) -> str:
    extra = f" = '{e.value}'" if e.value else ""
    alias = next(((n, why) for n, _w, why in e.aliases), None)
    also = f" ~ '{alias[0]}' ({alias[1]})" if alias else ""
    return (f"[{e.id}] {e.role}: {e.name or '(no name)'}{also}{extra}"
            + ("  (below/above - scrolls into view)" if e.offscreen else ""))


# ---------------------------------------------------------------------------------------------- unlabelled icons
def name_icons(els: list[El]) -> int:
    """Give unnamed on-screen controls a few words from the vision model: one picture with every such icon numbered,
    one answer. The vision model only says WHAT each icon is - WHERE it is still comes from Windows."""
    import cv2
    import numpy as np

    from .screen import grab

    todo = [e for e in els if e.role in ACTIONABLE and not e.offscreen and not e.name and not e.help
            and e.rect[2] <= 400 and e.rect[3] <= 300 and not any(why == "icon" for _, _, why in e.aliases)][:40]
    if not todo:
        return 0
    img, ox, oy = grab("")
    tiles: list[tuple[int, Any]] = []
    for i, e in enumerate(todo, 1):
        x, y, ww, hh = e.rect
        x0, y0 = max(0, x - ox - 6), max(0, y - oy - 6)
        crop = img[y0:y - oy + hh + 6, x0:x - ox + ww + 6]
        if crop.size == 0:
            continue
        k = 72 / max(crop.shape[:2])
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * k)), max(1, int(crop.shape[0] * k))),
                          interpolation=cv2.INTER_CUBIC)
        tile = np.full((112, 112, 3), 255, np.uint8)
        cx0 = (112 - crop.shape[1]) // 2
        tile[34:34 + crop.shape[0], cx0:cx0 + crop.shape[1]] = crop
        cv2.putText(tile, str(i), (4, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 200), 2, cv2.LINE_AA)
        cv2.rectangle(tile, (0, 0), (111, 111), (180, 180, 180), 1)
        tiles.append((i, tile))
    if not tiles:
        return 0
    cols = min(8, len(tiles))
    rows = -(-len(tiles) // cols)
    sheet = np.full((rows * 112, cols * 112, 3), 255, np.uint8)
    for j, (_, t) in enumerate(tiles):
        r, c = divmod(j, cols)
        sheet[r * 112:(r + 1) * 112, c * 112:(c + 1) * 112] = t
    prompt = ("Each numbered tile shows one small control from an app or web page (a button, icon or image). For each, "
              "say in 1-4 plain words what it is or does, e.g. '1: trash can delete', '2: plus add', '3: settings "
              "gear', '4: close X', '5: toggle switch', '6: teal picture'. One line per number, nothing else.")
    text = _run_async(_see([sheet], prompt))
    return apply_icon_names(todo, text)


def apply_icon_names(todo: list[El], text: str) -> int:
    got = 0
    for m in re.finditer(r"(?m)^\W*(\d+)\s*[:.)\-]\s*(.+?)\s*$", text):
        i = int(m.group(1))
        if 1 <= i <= len(todo):
            todo[i - 1].aliases.append((m.group(2).strip(" .*'\"")[:40].lower(), 0.82, "icon"))
            got += 1
    return got


async def _see(images, prompt: str) -> str:
    from ..vision import see

    return await see.ask(images, prompt, max_tokens=500, max_side=1400)


def _run_async(coro, timeout: float = 35.0):
    """Run a coroutine from a tool's worker thread on Jarvis's own event loop (its shared HTTP client lives there)."""
    from ..events import bus

    loop = getattr(bus, "loop", None)
    if loop is not None and loop.is_running():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not loop:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return fut.result(timeout)
            except Exception:
                fut.cancel()
                raise
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---------------------------------------------------------------------------------------------- tools
@tool(risk="low", timeout=60, tags=["buttons", "links", "elements", "what can i click", "page", "ui", "controls"],
      examples=["ui_elements(window='chrome', find='cart')", "ui_elements(window='spotify', icons=true)"])
def ui_elements(window: str = "", find: str = "", kind: str = "", limit: int = 60, icons: bool = False) -> str:
    """List the buttons, links, images, menu items and text boxes of an app or web page by name - the WHOLE page,
    also what's further down - so you know exactly what you can click with ui_click. `find` narrows it to names like
    that; `kind` to button / link / image / edit / menuitem / tab / checkbox. `window` = app ('chrome', 'spotify'...;
    default: the one in front). icons=true also names unlabelled icon buttons by sight."""
    with _ui_thread():
        els, w = scan(window)
        if icons or (find and not any(score(e, find) >= 62 for e in els)):
            try:
                name_icons(els)
            except Exception as e:
                log.info("naming icons failed: %s", e)
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


@tool(risk="medium", timeout=90, tags=["click", "press", "button", "link", "tap", "open", "right click", "select", "check"],
      examples=["ui_click(target='Add to cart', window='chrome')", "ui_click(target='Sign in button')",
                "ui_click(target='the cat picture', window='chrome', button='right')",
                "ui_click(target='the + next to Quantity')", "ui_click(target='trash can icon')"])
def ui_click(target: str, window: str = "", button: str = "left", double: bool = False, nth: int = 1) -> dict:
    """Click a button / link / image / menu item / tab / checkbox BY ITS NAME in any app or web page - exact, no
    vision needed. Finds it anywhere on the page (scrolls down to it if needed) and clicks its middle. button='right'
    for a context menu (then ui_click the menu item by name). `nth`=2 for the second match. Use this FIRST for
    clicking. Unlabelled icons work too: say what's next to it or what it looks like ('the + next to Quantity',
    'the trash can icon', 'the gear')."""
    from .screen import _mouse, focus

    deadline = time.time() + 75                    # the tool gives up at 90 s: never click after that
    with _ui_thread():
        els, w = scan(window)
        el, close = best(els, target, max(1, int(nth)))
        if el is None:                            # nothing by that name: maybe it's an icon nobody labelled
            try:
                if name_icons(els):
                    el, close = best(els, target, max(1, int(nth)))
            except Exception as e:
                log.info("naming icons failed: %s", e)
        if el is not None and time.time() > deadline:
            raise ToolError("Took too long finding it - not clicking late.", hint="Try again.")
        if el is None:
            raise ToolError(f"Nothing called '{target}' here.",
                            hint="Closest: " + "; ".join(_describe(e) for e in close[:5]) + ". Or ui_elements to see all.")
        if window:
            focus(window)
            time.sleep(0.15)
        if el.offscreen:
            _scroll_into_view(el, w)
        x, y, ww, hh = _visible_rect(el)
        if ww <= 0 or hh <= 0:
            raise ToolError(f"'{el.name or target}' is there but not visible right now.",
                            hint="It may be in a closed menu or tab.")
        cx, cy = x + ww // 2, y + hh // 2
        if w is not None and not _on_top(cx, cy, w.hwnd):
            # another window covers that spot (Windows doesn't always let us bring this one to the front): a mouse
            # click would land in the wrong app, so press it through accessibility instead - or stop
            if button != "right" and not double and _invoke(el):
                return {"clicked": _describe(el), "how": "invoked (the window was covered, no mouse needed)"}
            raise ToolError(f"Another window is covering '{el.name or target}', so clicking there would hit the wrong app.",
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

    with _ui_thread():
        els, w = scan(window)
        edits = [e for e in els if e.role in ("Edit", "ComboBox", "Document")]
        el, _close = best(edits or els, field + " box")
        if el is None:
            raise ToolError(f"No text box called '{field}' here.",
                            hint="Text boxes: " + "; ".join(_describe(e) for e in edits[:8]))
        if el.offscreen:
            _scroll_into_view(el, w)
        with contextlib.suppress(Exception):
            el.ctl.SetFocus()
        set_ok = False
        with contextlib.suppress(Exception):          # straight into the box: no keystrokes that could land elsewhere
            el.ctl.GetValuePattern().SetValue(text)
            set_ok = True
        x, y, ww, hh = _visible_rect(el)
        front = w is None or _on_top(x + ww // 2, y + hh // 2, w.hwnd)
        if not set_ok:
            if not front:
                raise ToolError(f"Another window is covering '{el.name or field}'.",
                                hint="Bring that window to the front first.")
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
