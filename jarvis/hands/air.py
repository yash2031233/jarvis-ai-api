"""Hand control beyond Jarvis's window.

Air mouse: the page tracks your hand (MediaPipe, in the app - nothing leaves the computer) and sends where the cursor
should be; this moves the REAL mouse, so you can use any app by hand:
    cursor            the point between thumb and index fingertip (steady while you pinch)
    pinch (index)     left click; hold it and move = drag
    pinch (middle)    right click
    fist + move       scroll
Gesture shortcuts: hold a pose for a moment and it runs whatever you set in Settings → Hand control (or ask Jarvis):
    thumbs_up, thumbs_down, peace, rock, three, shaka
An action is a built-in (play_pause, next_track, prev_track, mute, volume_up, volume_down, screenshot, show_desktop,
switch_window, talk, none), "say: <anything you'd say to Jarvis>", or "skill: <saved skill>".
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from .. import config
from ..events import bus
from .registry import ToolError, tool

log = logging.getLogger(__name__)

GESTURES = {
    "thumbs_up": "👍 thumbs up", "thumbs_down": "👎 thumbs down", "peace": "✌️ peace sign",
    "rock": "🤘 rock sign (index + pinky)", "three": "three fingers up", "shaka": "🤙 shaka (thumb + pinky)",
}
DEFAULT_GESTURES = {"thumbs_up": "play_pause", "thumbs_down": "mute", "peace": "screenshot", "rock": "next_track",
                    "three": "prev_track", "shaka": "talk"}
BUILTINS = {
    "play_pause": "play / pause music", "next_track": "next track", "prev_track": "previous track",
    "mute": "mute / unmute", "volume_up": "volume up", "volume_down": "volume down",
    "screenshot": "take a screenshot", "show_desktop": "show the desktop", "switch_window": "switch window (Alt+Tab)",
    "talk": "start listening (like the mic button)", "none": "nothing",
}


# ---------------------------------------------------------------------------------------------- the real mouse
class _Mouse:
    """Moves / clicks the system cursor. Positions come in as 0..1 across all monitors."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self._ctl = None

    def _bounds(self) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            import ctypes

            m = ctypes.windll.user32.GetSystemMetrics
            return m(76), m(77), max(1, m(78)), max(1, m(79))   # the virtual screen: every monitor
        try:
            import mss

            with mss.mss() as s:
                v = s.monitors[0]
            return v["left"], v["top"], v["width"], v["height"]
        except Exception:
            return 0, 0, 1920, 1080

    def ctl(self):
        if self._ctl is None:
            try:
                from pynput.mouse import Button, Controller
            except ImportError:
                raise ToolError("The air mouse needs pynput: pip install pynput")
            self._ctl, self._btn = Controller(), Button
        return self._ctl

    def move(self, x: float, y: float) -> tuple[int, int]:
        left, top, w, h = self._bounds()
        px = left + int(max(0.0, min(1.0, x)) * (w - 1))
        py = top + int(max(0.0, min(1.0, y)) * (h - 1))
        self.ctl().position = (px, py)
        return px, py

    def press(self, button: str, down: bool) -> None:
        c = self.ctl()
        b = self._btn.right if button == "right" else self._btn.left
        if down and button not in self.down:
            c.press(b)
            self.down.add(button)
        elif not down and button in self.down:
            c.release(b)
            self.down.discard(button)

    def click(self, button: str) -> None:
        c = self.ctl()
        c.click(self._btn.right if button == "right" else self._btn.left, 1)

    def scroll(self, notches: int) -> None:
        if notches:
            self.ctl().scroll(0, max(-15, min(15, int(notches))))

    def release_all(self) -> None:
        for b in list(self.down):
            self.press(b, False)


mouse = _Mouse()


def handle_air(msg: dict[str, Any]) -> None:
    """One message from the hand tracker: {ev: move|down|up|click|scroll|release, x, y, button, notches}."""
    if not config.store.load().air_mouse and msg.get("ev") != "release":
        return
    ev = msg.get("ev")
    try:
        if "x" in msg and "y" in msg:
            mouse.move(float(msg["x"]), float(msg["y"]))
        if ev == "down":
            mouse.press(str(msg.get("button", "left")), True)
        elif ev == "up":
            mouse.press(str(msg.get("button", "left")), False)
        elif ev == "click":
            mouse.click(str(msg.get("button", "left")))
        elif ev == "scroll":
            mouse.scroll(int(msg.get("notches", 0)))
        elif ev == "release":
            mouse.release_all()
    except ToolError as e:
        bus.emit("notice", level="warn", text=str(e))
    except Exception as e:
        log.debug("air mouse: %s", e)


# ---------------------------------------------------------------------------------------------- gesture shortcuts
def mapping() -> dict[str, str]:
    m = dict(DEFAULT_GESTURES)
    m.update({k: v for k, v in (config.store.load().gestures or {}).items() if k in GESTURES})
    return m


async def run_action(action: str, *, voice=None) -> str:
    """Do what a gesture is set to. Returns what happened (for the toast)."""
    from . import media

    a = (action or "none").strip()
    low = a.lower()
    if low.startswith("say:"):
        from ..agent.engine import agent

        text = a.split(":", 1)[1].strip()
        if text:
            asyncio.create_task(agent.handle(text, source="gesture"))
        return f"“{text}”"
    if low.startswith("skill:"):
        from ..agent.engine import agent

        name = a.split(":", 1)[1].strip()
        asyncio.create_task(agent.handle(f"run my {name} skill", source="gesture"))
        return f"skill {name}"
    if low in ("play_pause", "next_track", "prev_track"):
        await asyncio.to_thread(media.media_control, {"play_pause": "play_pause", "next_track": "next",
                                                     "prev_track": "previous"}[low])
    elif low == "mute":
        await asyncio.to_thread(media.set_volume, None, None, True)
    elif low in ("volume_up", "volume_down"):
        await asyncio.to_thread(media.set_volume, None, 10 if low == "volume_up" else -10, None)
    elif low == "screenshot":
        from .screen import screenshot

        shot = await asyncio.to_thread(screenshot)
        bus.emit("notice", level="info", text=f"Screenshot saved: {shot['path']}")
    elif low in ("show_desktop", "switch_window"):
        await asyncio.to_thread(_keys, low)
    elif low == "talk":
        if voice is not None and getattr(voice, "status", "") == "ready":
            voice.push_to_talk()
        else:
            return "voice isn't ready"
    elif low in ("none", ""):
        return "nothing"
    else:
        raise ToolError(f"Unknown gesture action '{action}'.")
    return BUILTINS.get(low, low)


def _keys(what: str) -> None:
    from pynput.keyboard import Controller, Key

    k = Controller()
    mod = Key.cmd if what == "show_desktop" else Key.alt
    with k.pressed(mod):
        k.tap("d" if what == "show_desktop" else Key.tab)


async def handle_gesture(name: str, voice=None) -> None:
    if name not in GESTURES:
        return
    action = mapping().get(name, "none")
    if action == "none":
        return
    try:
        did = await run_action(action, voice=voice)
        bus.emit("gesture", name=name, label=GESTURES[name], did=did)
    except Exception as e:
        bus.emit("notice", level="warn", text=f"{GESTURES[name]}: {e}")


@tool(risk="low", tags=["gesture", "hand", "hands", "air mouse", "hand control", "shortcut", "thumbs up"],
      examples=["hand_gestures(action='list')", "hand_gestures(action='set', gesture='peace', does='next_track')",
                "hand_gestures(action='set', gesture='rock', does='say: open spotify')",
                "hand_gestures(action='air_mouse', on=true)"])
def hand_gestures(action: str = "list", gesture: str = "", does: str = "", on: bool | None = None) -> dict:
    """Hand-control shortcuts and the air mouse. list = what each pose does; set = give a pose (thumbs_up,
    thumbs_down, peace, rock, three, shaka) an action: a built-in (play_pause, next_track, prev_track, mute,
    volume_up, volume_down, screenshot, show_desktop, switch_window, talk, none), 'say: <a command for Jarvis>' or
    'skill: <name>'; reset = defaults; air_mouse on=true/false = the hand moves the real mouse in every app (pinch =
    click, middle-finger pinch = right click, fist + move = scroll)."""
    a = action.lower().strip()
    if a == "set":
        g = gesture.lower().strip().replace(" ", "_").replace("-", "_")
        g = {"thumbs-up": "thumbs_up", "thumb_up": "thumbs_up", "thumb_down": "thumbs_down", "victory": "peace",
             "horns": "rock", "call_me": "shaka", "hang_loose": "shaka"}.get(g, g)
        if g not in GESTURES:
            raise ToolError(f"Unknown pose '{gesture}'.", hint="Poses: " + ", ".join(GESTURES))
        d = does.strip()
        if not d:
            raise ToolError("Say what it should do in `does`.")
        if d.lower() not in BUILTINS and not d.lower().startswith(("say:", "skill:")):
            d = "say: " + d                     # anything else is a command for Jarvis
        cur = dict(config.store.load().gestures or {})
        cur[g] = d
        config.store.update(gestures=cur)
    elif a == "reset":
        config.store.update(gestures={})
    elif a in ("air_mouse", "airmouse", "mouse"):
        config.store.update(air_mouse=bool(on) if on is not None else not config.store.load().air_mouse)
        bus.emit("settings_changed")
    elif a != "list":
        raise ToolError(f"Unknown action '{action}'.", hint="list, set, reset, air_mouse")
    m = mapping()
    return {"air_mouse": config.store.load().air_mouse,
            "gestures": {GESTURES[g]: (BUILTINS.get(v, v)) for g, v in m.items()},
            "note": "Hand control has to be on (the hand button, or 'hands_on') for any of this to work."}
