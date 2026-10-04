"""Media keys + volume, cross-platform without extra dependencies."""

from __future__ import annotations

import time

from .osutil import IS_LINUX, IS_MAC, IS_WIN, run, which
from .registry import ToolError, tool

VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
      "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF}


def _win_key(vk: int, times: int = 1) -> None:
    import ctypes

    u = ctypes.windll.user32
    for _ in range(times):
        u.keybd_event(vk, 0, 1, 0)
        u.keybd_event(vk, 0, 3, 0)


@tool(risk="low", tags=["play", "pause", "resume", "next", "skip", "previous", "song", "track", "music", "media"],
      examples=["media_control(action='next')"])
def media_control(action: str) -> str:
    """Control media playback: play_pause | next | previous | stop."""
    a = action.lower().replace(" ", "_").replace("-", "_")
    a = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "skip": "next",
         "prev": "previous", "back": "previous"}.get(a, a)
    if a not in ("play_pause", "next", "previous", "stop"):
        raise ToolError(f"Unknown media action '{action}'.", hint="Use play_pause, next, previous or stop.")
    if IS_WIN:
        _win_key(VK[a])
    elif IS_MAC:
        key = {"play_pause": "playpause", "next": "next track", "previous": "previous track", "stop": "pause"}[a]
        r = run(["osascript", "-e", f'tell application "Music" to {key}'])
        if r.returncode != 0:
            run(["osascript", "-e", f'tell application "Spotify" to {key}'])
    else:
        if not which("playerctl"):
            raise ToolError("Media control needs playerctl on Linux.", hint="sudo apt install playerctl")
        run(["playerctl", {"play_pause": "play-pause", "previous": "previous"}.get(a, a)])
    return f"Media: {a.replace('_', '/')}"


@tool(risk="low", tags=["volume", "louder", "quieter", "mute", "unmute", "sound", "audio"],
      examples=["set_volume(level=40)", "set_volume(change=-10)", "set_volume(mute=true)"])
def set_volume(level: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    """Set system volume to an absolute level (0-100), change it by a relative amount, or mute/unmute."""
    if IS_WIN:
        if mute is not None:
            _win_key(VK["mute"])
            return "Toggled mute."
        if level is not None:
            level = max(0, min(100, level))
            _win_key(VK["volume_down"], 50)  # each step = 2%
            time.sleep(0.02)
            _win_key(VK["volume_up"], round(level / 2))
            return f"Volume set to {level}%."
        if change is not None:
            _win_key(VK["volume_up" if change > 0 else "volume_down"], max(1, abs(change) // 2))
            return f"Volume {'up' if change > 0 else 'down'} {abs(change)}%."
    elif IS_MAC:
        if mute is not None:
            run(["osascript", "-e", f"set volume output muted {str(mute).lower()}"])
            return "Muted." if mute else "Unmuted."
        if level is None and change is not None:
            cur = run(["osascript", "-e", "output volume of (get volume settings)"]).stdout.strip()
            level = int(cur or 50) + change
        if level is not None:
            level = max(0, min(100, level))
            run(["osascript", "-e", f"set volume output volume {level}"])
            return f"Volume set to {level}%."
    elif IS_LINUX:
        if which("pactl"):
            sink = "@DEFAULT_SINK@"
            if mute is not None:
                run(["pactl", "set-sink-mute", sink, "1" if mute else "0"])
                return "Muted." if mute else "Unmuted."
            if level is not None:
                run(["pactl", "set-sink-volume", sink, f"{max(0, min(100, level))}%"])
                return f"Volume set to {level}%."
            if change is not None:
                run(["pactl", "set-sink-volume", sink, f"{'+' if change > 0 else '-'}{abs(change)}%"])
                return f"Volume changed by {change}%."
        raise ToolError("Volume control needs pactl (PulseAudio/PipeWire).")
    raise ToolError("Specify level, change or mute.")
