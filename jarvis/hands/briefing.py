"""Morning briefing + nightly diary.

briefing  the day at a glance - weather, today's reminders and to-dos, tests/deadlines from memory, finished background
          jobs - gathered without a model call, so it's instant and never wrong about the numbers. Sent automatically
          at `briefing_time` (while Jarvis is running) or on request.
diary     every night (`diary_time`) Jarvis writes the day into the vault as Diary/<date>.md - what you talked about,
          what got done, what he learned - in his own words, linking the notes it mentions. Ask "what did I do on
          Tuesday?" and he reads it back.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from .. import config
from .registry import ToolError, tool


async def gather() -> dict[str, Any]:
    now = datetime.now()
    out: dict[str, Any] = {"date": now.strftime("%A, %B %d").replace(" 0", " ")}
    try:
        from .weather import get_weather

        w = await get_weather(city=config.store.load().home_city, days=1)
        f = w["forecast"][0]
        out["weather"] = {"now": w["now"]["temp"], "high": f["high"], "low": f["low"], "conditions": f["conditions"],
                          "rain_chance": f["rain_chance"], "units": w["units"], "place": w["place"]}
    except Exception:
        pass
    from .timers import list_timers_sync

    out["reminders"] = [t for t in list_timers_sync() if t.get("label")]
    from ..memory import vault

    out["todo"] = [vault._strip_date(f) for f in vault.facts("To-do")][-8:]
    today_words = {now.strftime("%A").lower(), "today", now.strftime("%b %d").lower().replace(" 0", " "),
                   now.strftime("%B %d").lower().replace(" 0", " "), now.strftime("%Y-%m-%d"), f"{now.month}/{now.day}"}
    due = []
    for n in vault.user_notes():
        for f in vault.facts(n):
            fl = f.lower()
            if re.search(r"\b(test|quiz|exam|due|deadline|appointment|meeting|presentation|practice|game)\b", fl) \
                    and any(w in fl for w in today_words):
                due.append(f"{n}: {vault._strip_date(f)}")
    out["today"] = due[:8]
    try:
        from ..agent import jobs

        out["jobs_done"] = [j["task"][:80] for j in jobs.all_jobs()
                            if j["status"] == "done" and time.time() - j.get("finished", 0) < 86400][:3]
    except Exception:
        out["jobs_done"] = []
    return out


def compose(d: dict[str, Any]) -> str:
    name = config.store.load().user_name
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    parts = [f"{greet}{', ' + name if name else ''}. It's {d['date']}."]
    w = d.get("weather")
    if w:
        deg = "°" + w["units"]
        s = f"It's {round(w['now'])}{deg} now, {w['conditions']}, with a high of {round(w['high'])}{deg}"
        if w.get("rain_chance") and w["rain_chance"] >= 40:
            s += f" and a {w['rain_chance']}% chance of rain"
        parts.append(s + ".")
    if d.get("today"):
        parts.append("On today: " + "; ".join(d["today"]) + ".")
    if d.get("reminders"):
        parts.append("Reminders: " + "; ".join(f"{r['label']} in {r['remaining']}" for r in d["reminders"]) + ".")
    if d.get("todo"):
        parts.append(f"Your to-do list has {len(d['todo'])} item{'s' if len(d['todo']) != 1 else ''}: "
                     + "; ".join(d["todo"][:4]) + ("…" if len(d["todo"]) > 4 else "") + ".")
    if d.get("jobs_done"):
        parts.append("Finished in the background: " + "; ".join(d["jobs_done"]) + ".")
    if len(parts) == 1:
        parts.append("Nothing on the calendar that I know of.")
    return " ".join(parts)


@tool(risk="low", tags=["briefing", "morning", "my day", "today", "what's on", "agenda", "summary of my day"],
      examples=["briefing()"])
async def briefing() -> dict:
    """The day at a glance: weather, today's tests/deadlines from memory, reminders, to-do list, finished background
    jobs. Use for 'morning briefing', 'what's my day look like', 'what do I have today'. Returns `text` ready to say."""
    d = await gather()
    return {"text": compose(d), "details": d}


# ------------------------------------------------------------------ diary
DIARY_SYSTEM = """You are Jarvis writing your private diary entry about the day you spent helping {name}.
Write 1-3 short paragraphs in first person, warm but concise: what {name} worked on and talked about, what got
done, anything notable or worth remembering. Link notes that exist with [[Note title]] when you mention them.
Only write what the conversations actually show - never invent details (colours, outcomes, feelings) that aren't
there. Refer to {name} by name or as "they" - don't assume pronouns. No headings, no lists. If very little
happened, write two sentences."""


def _diary_path(day: datetime):
    from ..memory import vault

    d = vault.root() / "Diary"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{day.strftime('%Y-%m-%d')}.md"


async def write_diary(day: datetime | None = None) -> str | None:
    from ..memory import vault
    from ..memory.store import memory

    day = day or datetime.now()
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = start + 86400
    msgs = [m for m in memory.messages_since(0, 100000) if start <= m["ts"] < end and m["content"]]
    if not msgs:
        return None
    convo = "\n".join(f"{time.strftime('%H:%M', time.localtime(m['ts']))} {m['role']}: {m['content'][:400]}"
                      for m in msgs)
    log_note = vault.root() / f"{vault.LOG_NOTE}.md"
    learned = []
    if log_note.exists():
        ds = day.strftime("%Y-%m-%d")
        learned = [ln[2:] for ln in log_note.read_text("utf-8").splitlines() if ln.startswith(f"- {ds}")]
    name = config.store.load().user_name or "the user"
    from ..brain.client import brain

    text, _ = await brain.complete([
        {"role": "system", "content": DIARY_SYSTEM.format(name=name)},
        {"role": "user", "content": f"Date: {day.strftime('%A %B %d, %Y')}\nExisting notes: "
                                    f"{', '.join(vault.notes()[:80])}\n\nConversations:\n{convo[-14000:]}\n\n"
                                    f"Things filed into memory today:\n" + ("\n".join(learned) or "none")},
    ], max_tokens=600, temperature=0.6)
    p = _diary_path(day)
    p.write_text(f"# {day.strftime('%A %B %d, %Y')}\n\n{text.strip()}\n", "utf-8")
    return str(p)


@tool(risk="low", tags=["diary", "what did i do", "yesterday", "last week", "on monday", "day", "journal"],
      examples=["diary(date='yesterday')", "diary(date='2026-10-01')", "diary(date='last tuesday')"])
async def diary(date: str = "yesterday", write: bool = False) -> dict:
    """Read Jarvis's diary entry for a day (what the user did/talked about): date = today | yesterday | a weekday
    ('last tuesday') | YYYY-MM-DD. write=true writes (or rewrites) that day's entry now."""
    day = _parse_day(date)
    p = _diary_path(day)
    if write or (not p.exists() and day.date() == datetime.now().date()):
        made = await write_diary(day)
        if not made:
            raise ToolError(f"Nothing happened on {day.strftime('%A %B %d')} to write about.")
    if not p.exists():
        raise ToolError(f"No diary entry for {day.strftime('%A %B %d')}.", hint="write=true to write one now.")
    return {"date": day.strftime("%Y-%m-%d"), "entry": p.read_text("utf-8")}


def _parse_day(s: str) -> datetime:
    s = (s or "yesterday").lower().strip()
    now = datetime.now()
    if s in ("today", "tonight"):
        return now
    if s == "yesterday":
        return now - timedelta(days=1)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, d in enumerate(days):
        if d in s:
            back = (now.weekday() - i) % 7 or 7
            return now - timedelta(days=back)
    m = re.match(r"^(\d+)\s+days?\s+ago$", s)
    if m:
        return now - timedelta(days=int(m.group(1)))
    raise ToolError(f"Can't read the date '{s}'.", hint="today, yesterday, last tuesday, 3 days ago or YYYY-MM-DD")
