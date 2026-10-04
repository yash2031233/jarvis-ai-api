"""Timers, alarms and reminders. Fire as UI notifications + spoken alerts."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta

from ..events import bus
from .registry import ToolError, tool

_timers: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}


def _fmt(secs: float) -> str:
    secs = int(secs)
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    parts = [f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s" if s and not h else ""]
    return " ".join(p for p in parts if p) or "0s"


async def _fire(tid: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    t = _timers.pop(tid, None)
    _tasks.pop(tid, None)
    if t:
        text = f"Reminder: {t['label']}" if t["kind"] == "reminder" else f"Timer done: {t['label']}"
        bus.emit("alert", text=text, speak=True)


def _schedule(kind: str, label: str, delay: float) -> dict:
    tid = uuid.uuid4().hex[:6]
    t = {"id": tid, "kind": kind, "label": label, "due": time.time() + delay}
    _timers[tid] = t
    _tasks[tid] = asyncio.get_running_loop().create_task(_fire(tid, delay))
    bus.emit("timers", timers=list_timers_sync())
    return t


def parse_clock(at: str) -> float:
    """'7:30 pm' / '19:30' / 'tomorrow 8am' → seconds from now."""
    s = at.lower().strip()
    tomorrow = "tomorrow" in s
    s = s.replace("tomorrow", "").replace("at", "").strip()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        raise ToolError(f"Can't understand time '{at}'.", hint="Use e.g. '7:30 pm' or '19:30'.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    now = datetime.now()
    due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if tomorrow:
        due += timedelta(days=1)
    elif due <= now:
        due += timedelta(days=1)
    return (due - now).total_seconds()


@tool(risk="low", tags=["timer", "countdown", "minutes", "seconds"],
      examples=["set_timer(seconds=300, label='pasta')"])
async def set_timer(seconds: int, label: str = "timer") -> str:
    """Start a countdown timer."""
    if seconds <= 0:
        raise ToolError("Duration must be positive.")
    _schedule("timer", label, seconds)
    return f"Timer set for {_fmt(seconds)} ({label})."


@tool(risk="low", tags=["remind", "reminder", "alarm", "wake", "later", "at"],
      examples=["set_reminder(text='call mom', in_minutes=30)", "set_reminder(text='meeting', at='3:30 pm')"])
async def set_reminder(text: str, in_minutes: float | None = None, at: str | None = None) -> str:
    """Set a reminder/alarm either in N minutes or at a clock time ('7:30 pm', 'tomorrow 8am')."""
    if in_minutes is not None:
        delay = in_minutes * 60
    elif at:
        delay = parse_clock(at)
    else:
        raise ToolError("Give in_minutes or at.")
    _schedule("reminder", text, delay)
    when = (datetime.now() + timedelta(seconds=delay)).strftime("%I:%M %p").lstrip("0")
    return f"I'll remind you to {text} at {when}."


def list_timers_sync() -> list[dict]:
    now = time.time()
    return [{"id": t["id"], "kind": t["kind"], "label": t["label"], "remaining": _fmt(t["due"] - now)}
            for t in sorted(_timers.values(), key=lambda t: t["due"])]


@tool(risk="low", tags=["timers", "reminders", "list", "remaining", "how long"])
def list_timers() -> list[dict]:
    """List active timers and reminders with time remaining."""
    return list_timers_sync() or [{"info": "no active timers"}]


@tool(risk="low", tags=["cancel", "stop", "timer", "reminder", "delete"])
def cancel_timer(id_or_label: str = "") -> str:
    """Cancel a timer/reminder by id or label (empty = cancel the most recent)."""
    if not _timers:
        raise ToolError("No active timers.")
    target = None
    if id_or_label:
        for t in _timers.values():
            if t["id"] == id_or_label or id_or_label.lower() in t["label"].lower():
                target = t
                break
    else:
        target = list(_timers.values())[-1]  # most recently created
    if not target:
        raise ToolError(f"No timer matching '{id_or_label}'.", hint="Use list_timers.")
    _timers.pop(target["id"], None)
    task = _tasks.pop(target["id"], None)
    if task:
        task.cancel()
    bus.emit("timers", timers=list_timers_sync())
    return f"Cancelled {target['kind']} '{target['label']}'."


@tool(risk="low", tags=["quiet", "shh", "stop interrupting", "do not disturb", "pause", "proactive"],
      examples=["quiet(minutes=120)", "quiet(minutes=0)"])
def quiet(minutes: int = 120) -> str:
    """Stop Jarvis speaking up on his own for a while (minutes; 0 = allow it again)."""
    from .. import proactive

    until = proactive.pause(minutes)
    if not until:
        return "I'll speak up again when something's worth it."
    return f"I'll stay quiet until {datetime.fromtimestamp(until).strftime('%I:%M %p').lstrip('0')} unless you ask."
