"""Everything the app's screens can do, as tools - so "talk slower", "switch to the local model", "add my door camera",
"text me that address", "what did I ask you about the essay yesterday" or "show my history" work by just asking."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import config
from ..events import bus
from .registry import ToolError, tool

# What the model may change, with a short description of each. Security switches (shell access, tool permissions)
# and secrets (API keys, camera passwords, bot token) are deliberately not here - those stay in Settings.
SETTABLE: dict[str, str] = {
    "user_name": "what Jarvis calls you",
    "personality": "Jarvis's personality / how he talks",
    "provider": "model provider: nvidia | lmstudio | ollama | custom",
    "model": "the model id to use",
    "temperature": "0-1, higher = more creative",
    "voice_enabled": "voice on/off (listening + speaking)",
    "tts_voice": "speaking voice id (see `voices`)",
    "tts_speed": "speaking speed, 0.6-1.6",
    "speaker_device": "speaker/headphones to talk through, by name (empty = system default)",
    "wake_word": "listen for 'Hey Jarvis'",
    "fast_path": "instant local commands without a model call",
    "proactive": "speak up about rain, tomorrow's tests, battery…",
    "heartbeat_min": "minutes between check-ins (0 = off)",
    "quiet_start": "quiet hours start, HH:MM",
    "quiet_end": "quiet hours end, HH:MM",
    "home_city": "city for weather alerts (empty = your location)",
    "briefing_time": "morning briefing time HH:MM (empty = off)",
    "diary_time": "nightly diary time HH:MM (empty = off)",
    "memory_keeper": "file facts from conversations into memory by itself",
    "learn": "learn lessons from experience",
    "cad_review": "visual self-check of 3D designs",
    "bed_mm": "3D printer build volume [x, y, z] in mm",
    "printer_kind": "3D printer type: flashforge | moonraker | octoprint (empty = none)",
    "printer_host": "3D printer address (empty = find it on the network)",
    "printer_model": "slicer printer profile, e.g. 'Flashforge AD5X 0.4 nozzle'",
    "printer_filament": "filament profile to slice with, e.g. 'PLA Basic'",
    "slicer_path": "path to the OrcaSlicer-family slicer (empty = find it)",
    "auto_webcam": "use the first webcam without setting it up",
    "nav_voice": "speak turn-by-turn directions",
    "nav_units": "imperial | metric for directions",
    "index_folders": "folders searched by content (list of paths)",
    "max_agent_steps": "max tool rounds per request",
}


def _coerce(name: str, value: Any) -> Any:
    cur = getattr(config.store.load(), name)
    if isinstance(cur, bool):
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "on", "yes", "1", "enable", "enabled"):
                return True
            if v in ("false", "off", "no", "0", "disable", "disabled"):
                return False
            raise ToolError(f"{name} needs on/off.")
        return bool(value)
    if isinstance(cur, int) and not isinstance(cur, bool):
        return int(float(value))
    if isinstance(cur, float):
        return float(value)
    if isinstance(cur, list) and isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return value


@tool(risk="low", tags=["settings", "setting", "preference", "voice", "speed", "slower", "faster", "model", "switch model",
                        "quiet hours", "briefing time", "turn off", "turn on", "mute", "personality", "call me"],
      examples=["settings(action='set', name='tts_speed', value='0.9')", "settings(action='get')",
                "settings(action='set', name='model', value='moonshotai/kimi-k3')", "settings(action='voices')"])
async def settings(action: str = "get", name: str = "", value: str = "") -> dict:
    """Read or change Jarvis's own settings. action: get = current values (optionally one `name`);
    set = change `name` to `value`; voices = the speaking voices available; models = the provider's models.
    Settable: user_name, personality, provider, model, temperature, voice_enabled, tts_voice, tts_speed, wake_word,
    fast_path, proactive, heartbeat_min, quiet_start, quiet_end, home_city, briefing_time, diary_time, memory_keeper,
    learn, cad_review, bed_mm, auto_webcam, nav_voice, nav_units, index_folders, max_agent_steps.
    API keys, passwords and security switches can only be changed in the Settings screen."""
    a = action.strip().lower()
    s = config.store.load()
    if a == "get":
        if name:
            if name not in SETTABLE:
                raise ToolError(f"Unknown or private setting '{name}'.", hint="settable: " + ", ".join(SETTABLE))
            return {name: getattr(s, name), "means": SETTABLE[name]}
        return {k: getattr(s, k) for k in SETTABLE if k != "personality"}
    if a == "voices":
        from ..voice.tts import POCKET_VOICES, VOICES, pocket_available

        return {"current": s.tts_voice, "voices": (list(POCKET_VOICES) if pocket_available() else []) + VOICES}
    if a == "models":
        from ..brain.client import brain

        try:
            ids = await brain.list_models()
        except Exception as e:
            raise ToolError(f"Couldn't list models: {e}")
        return {"current": s.model, "models": ids[:80]}
    if a != "set":
        raise ToolError("action must be get, set, voices or models.")
    if name not in SETTABLE:
        raise ToolError(f"'{name}' can't be changed by asking.", hint="settable: " + ", ".join(SETTABLE))
    v = _coerce(name, value)
    changes: dict[str, Any] = {name: v}
    if name == "provider":
        preset = config.PROVIDER_PRESETS.get(str(v))
        if not preset and v != "custom":
            raise ToolError("provider must be one of: " + ", ".join(list(config.PROVIDER_PRESETS) + ["custom"]))
        if preset:
            changes["base_url"] = preset["base_url"]
    try:
        new = config.store.update(**changes)
    except Exception as e:
        raise ToolError(f"Invalid value: {e}")
    try:
        from ..server.app import apply_settings

        apply_settings()
    except Exception:
        pass
    bus.emit("settings_changed", name=name)
    return {"set": name, "value": getattr(new, name), "was": getattr(s, name)}


@tool(risk="low", tags=["camera", "add camera", "ip camera", "webcam", "remove camera", "default camera", "rtsp"],
      examples=["camera_setup(action='add', name='Front door', kind='ip', url='rtsp://192.168.1.20:554/stream1')",
                "camera_setup(action='detect')", "camera_setup(action='default', name='Desk')"])
def camera_setup(action: str = "list", name: str = "", kind: str = "device", device: int = 0, url: str = "") -> dict:
    """Set up cameras. action: list; detect = find webcams plugged into this computer; add = a camera called `name`,
    `kind` device (a webcam: `device` index from detect) or ip (`url`: rtsp://…, http://…/video MJPEG, or a snapshot
    URL; its password goes to the OS keychain); remove `name`; default = make `name` the default camera."""
    from ..vision import cameras

    a = action.strip().lower()
    if a == "list":
        d = config.store.load().default_camera
        return {"cameras": [{**c.info(), "default": c.id == d} for c in cameras.hub.all()]}
    if a == "detect":
        return {"devices": cameras.detect_devices()}
    if a == "add":
        if not name:
            raise ToolError("add needs a `name`.")
        if kind == "ip" and not url:
            raise ToolError("An IP camera needs its `url`.")
        try:
            cam = cameras.add_camera(name, "ip" if kind == "ip" else "device", int(device), url)
        except Exception as e:
            raise ToolError(f"Couldn't add the camera: {e}")
        return {"added": {k: v for k, v in cam.items() if k != "url"}}
    cams = cameras.hub.all()
    match = next((c for c in cams if c.name.lower() == name.strip().lower() or c.id == name), None)
    if not match:
        raise ToolError(f"No camera called '{name}'.", hint="cameras: " + ", ".join(c.name for c in cams))
    if a == "remove":
        cameras.remove_camera(match.id)
        return {"removed": match.name}
    if a == "default":
        config.store.update(default_camera=match.id)
        return {"default": match.name}
    raise ToolError("action must be list, detect, add, remove or default.")


@tool(risk="medium", tags=["text me", "message me", "send to my phone", "telegram", "phone", "remind me on my phone"],
      examples=["message_phone(text='Empire State Building: 350 5th Ave, New York')"])
def message_phone(text: str) -> dict:
    """Send `text` to the user's phone through their paired Jarvis Telegram bot (Settings → Location)."""
    from .. import telegram

    if not telegram.chat_id() or not telegram.token():
        raise ToolError("No phone linked.", hint="Settings → Location: add a Telegram bot and pair it.")
    if not telegram.send(text):
        raise ToolError("Telegram didn't accept the message.")
    return {"sent": True}


@tool(risk="low", readonly=True,
      tags=["history", "conversation", "what did i say", "what did i ask", "earlier", "yesterday", "last time", "past chats"],
      examples=["conversation_history(query='essay')", "conversation_history(action='recent', limit=10)"])
def conversation_history(query: str = "", action: str = "search", limit: int = 12) -> dict:
    """Past conversations with Jarvis. action: search = messages containing `query` (newest first);
    recent = the last `limit` messages."""
    from ..memory.store import memory

    limit = max(1, min(int(limit), 50))
    rows = memory.history(limit) if action == "recent" or not query else memory.search(query, limit)
    return {"messages": [{"when": datetime.fromtimestamp(m["ts"]).strftime("%a %b %d %H:%M"), "role": m["role"],
                          "text": (m["content"] or "")[:400]} for m in rows]}


PANELS = ("history", "settings", "map", "cameras", "study", "model", "jobs", "hub", "hands_on", "hands_off", "close")


@tool(risk="low", tags=["show", "open", "panel", "history", "settings", "map", "screen", "close", "dashboard", "hub",
                        "hand control", "gestures", "holo"],
      examples=["show_panel(panel='history')", "show_panel(panel='map')", "show_panel(panel='close')"])
def show_panel(panel: str) -> dict:
    """Open one of the app's screens: history (conversation + tool calls + background jobs), settings, map,
    cameras (live view), study, model (the 3D part), jobs, hub (the dashboard: computer, network, printer, car,
    weather...); hands_on / hands_off = hand control (wave and pinch at the webcam to use the app); close = back
    to the orb."""
    p = panel.strip().lower()
    if p not in PANELS:
        raise ToolError(f"Unknown panel '{panel}'.", hint="one of: " + ", ".join(PANELS))
    bus.emit("ui_open", panel=p)
    return {"opened": p}


@tool(risk="low", timeout=60, tags=["find devices", "scan", "printer", "robot car", "camera", "detect", "network devices"],
      examples=["find_devices()"])
async def find_devices(setup: bool = True) -> dict:
    """Find the devices Jarvis can use on this computer and network - 3D printers, the robot car, webcams, IP
    cameras, microphones and speakers - each one checked to really be that device. With setup=true, anything not
    set up yet (or that moved to a new address) is set up."""
    import asyncio

    from .. import devices

    found = await asyncio.to_thread(devices.scan)
    if setup:
        found["set_up"] = devices.apply(found) or "nothing new to set up"
    return found
