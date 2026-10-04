"""Proactive Jarvis: he speaks up on his own when something is worth it - and stays quiet otherwise.

Two layers, run every few minutes while the app is open:
  rules      cheap checks, no model call: rain soon, a test/quiz/deadline tomorrow (from memory), a game running
             for hours, low battery
  heartbeat  (optional, every N minutes) the model gets a small picture of what's going on - time, the window in
             front, what's on screen (OCR), recently changed files, how long since the user spoke, timers, what
             it said on its own lately and whether that was welcome - and answers one question: is anything worth
             saying right now? Silence is the default.

Limits: quiet hours, at most one message per `proactive_gap_min`, each topic once per its own cooldown, and a pause
("quiet for 2 hours"). Every message is logged with whether the user engaged within 10 minutes; the heartbeat sees
those stats, so it calibrates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

import psutil

from . import config
from .events import bus

log = logging.getLogger(__name__)
LOG = config.DATA_DIR / "proactive.jsonl"
STATE = config.DATA_DIR / "proactive_state.json"
TICK = 60
GAMES = re.compile(r"(minecraft|javaw|robloxplayer|fortnite|valorant|leagueclient|league of legends|cs2|csgo|"
                   r"gta5|rocketleague|overwatch|apex|eldenring|dota2|r5apex|cod|destiny2|eurotrucks|fifa|fc2[45])",
                   re.I)


# ------------------------------------------------------------------ state + log
def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {}


def _save(**kw: Any) -> None:
    s = _state()
    s.update(kw)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s), "utf-8")


def _entries(hours: float = 72) -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    cut = time.time() - hours * 3600
    out = []
    for ln in LOG.read_text("utf-8").splitlines():
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("t", 0) >= cut:
            out.append(e)
    return out


def pause(minutes: float) -> float:
    until = time.time() + minutes * 60 if minutes > 0 else 0
    _save(quiet_until=until)
    return until


def _in_quiet_hours(now: datetime) -> bool:
    s = config.store.load()

    def t(x: str) -> int:
        h, m = (x or "0:0").split(":")
        return int(h) * 60 + int(m)

    a, b, cur = t(s.quiet_start), t(s.quiet_end), now.hour * 60 + now.minute
    return (a <= cur or cur < b) if a > b else (a <= cur < b)


def can_speak(topic: str, cooldown_min: float) -> bool:
    s = config.store.load()
    st = _state()
    now = time.time()
    if now < st.get("quiet_until", 0) or _in_quiet_hours(datetime.now()):
        return False
    if now - st.get("last_msg", 0) < s.proactive_gap_min * 60:
        return False
    if now - st.get("topics", {}).get(topic, 0) < cooldown_min * 60:
        return False
    return True


def deliver(text: str, topic: str, speak: bool = True) -> None:
    """Say it: in the app (and out loud), plus a desktop notification if the app isn't in front."""
    now = time.time()
    st = _state()
    topics = st.get("topics", {})
    topics[topic] = now
    _save(last_msg=now, topics=topics)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now, "topic": topic, "text": text}) + "\n")
    from .agent.engine import agent
    from .memory.store import memory

    memory.add_message("assistant", text, {"proactive": topic})
    agent.messages.append({"role": "assistant", "content": text})  # so "ok, quiz me" makes sense next
    bus.emit("proactive", text=text, topic=topic)
    if speak:
        agent._say(text)
    log.info("proactive [%s]: %s", topic, text)


def _mark_engagement() -> dict[str, int]:
    """An interruption counts as engaged if the user said something within 10 minutes of it."""
    from .memory.store import memory

    entries = _entries(24 * 14)
    if not entries:
        return {"engaged": 0, "ignored": 0}
    user_times = [m["ts"] for m in memory.messages_since(0, 100000) if m["role"] == "user"]
    engaged = ignored = 0
    for e in entries:
        if time.time() - e["t"] < 600:
            continue
        if any(e["t"] < u <= e["t"] + 600 for u in user_times):
            engaged += 1
        else:
            ignored += 1
    return {"engaged": engaged, "ignored": ignored}


# ------------------------------------------------------------------ rule checks (no model)
async def check_rain() -> str | None:
    if not can_speak("rain", 6 * 60):
        return None
    from .hands.weather import _locate
    from .hands.web import client

    s = config.store.load()
    try:
        lat, lon, place = await _locate(s.home_city)
        r = await client().get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "hourly": "precipitation_probability", "forecast_hours": 2,
            "timezone": "auto"})
        probs = r.json()["hourly"]["precipitation_probability"]
    except Exception:
        return None
    if probs and max(probs[:2]) >= 65:
        return f"Heads up: rain is likely in the next hour or so ({max(probs[:2])}%)."
    return None


def check_tomorrow() -> str | None:
    """A test / quiz / exam / deadline tomorrow, from memory - the evening before."""
    now = datetime.now()
    if not 17 <= now.hour < 21:
        return None
    from .memory import vault

    tom = now + timedelta(days=1)
    day_words = {tom.strftime("%A").lower(), "tomorrow", tom.strftime("%b %d").lower().replace(" 0", " "),
                 tom.strftime("%B %d").lower().replace(" 0", " "), tom.strftime("%Y-%m-%d"), f"{tom.month}/{tom.day}"}
    for n in vault.user_notes():
        for f in vault.facts(n):
            fl = f.lower()
            if re.search(r"\b(test|quiz|exam|due|deadline|presentation|essay|interview|appointment)\b", fl) and \
                    any(w in fl for w in day_words):
                key = f"tomorrow:{n}:{f[:40]}"
                if can_speak(key, 24 * 60):
                    return f"Reminder: {n} - {vault._strip_date(f)}. That's tomorrow."
    return None


_game_since: dict[int, float] = {}


def check_gaming() -> str | None:
    now = time.time()
    alive = set()
    name = ""
    for p in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            n = p.info["name"] or ""
            if GAMES.search(n):
                alive.add(p.info["pid"])
                _game_since.setdefault(p.info["pid"], p.info["create_time"] or now)
                name = name or n.removesuffix(".exe")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for pid in list(_game_since):
        if pid not in alive:
            _game_since.pop(pid)
    if not _game_since:
        return None
    hours = (now - min(_game_since.values())) / 3600
    if hours >= 3 and can_speak("gaming", 90):
        return f"You've been playing {name} for about {hours:.0f} hours. Maybe take a break?"
    return None


def check_battery() -> str | None:
    b = psutil.sensors_battery()
    if b and not b.power_plugged and b.percent <= 15 and can_speak("battery", 60):
        return f"Battery's at {round(b.percent)}% - you might want to plug in."
    return None


# ------------------------------------------------------------------ heartbeat (model decides)
HEARTBEAT_SYSTEM = """You are Jarvis, a desktop assistant, deciding whether to speak up UNPROMPTED.
You get a snapshot of what's happening. Interrupting is costly, so the default is silence. Speak ONLY when the
user would clearly thank you right now, e.g.:
- something they said they must do has a deadline within ~30 minutes and they seem to be doing something else
- an error, crash or failed job is on screen that they may not have noticed
- a long task they were waiting for just finished
- a reminder or commitment from the conversation is due
Do not narrate, do not plan, do not explain your decision.
Reply with JSON only: {"speak": false} or {"speak": true, "message": "1-2 natural sentences to say"}"""


def _recent_files(minutes: int = 15) -> list[str]:
    from .hands.osutil import user_folders

    cut = time.time() - minutes * 60
    out = []
    for k in ("downloads", "desktop", "documents"):
        d = user_folders().get(k)
        if not d:
            continue
        try:
            for p in d.iterdir():
                if p.is_file() and p.stat().st_mtime > cut:
                    out.append(f"{k}/{p.name}")
        except OSError:
            continue
    return out[:10]


async def heartbeat_once() -> str | None:
    if not can_speak("heartbeat", 0):
        return None
    from .agent.context import active_window
    from .memory.store import memory

    msgs = memory.history(30)
    last_user = max((m["ts"] for m in msgs if m["role"] == "user"), default=0)
    screen = ""
    try:
        from .hands.screen import grab
        from .vision import ocr

        img, _, _ = await asyncio.to_thread(grab, "")
        screen = ocr.text_of(await asyncio.to_thread(ocr.read_image, img))[:1500]
    except Exception:
        pass
    from .hands.timers import list_timers_sync

    stats = _mark_engagement()
    said = [f"- {time.strftime('%H:%M', time.localtime(e['t']))} {e['text']}" for e in _entries(6)]
    snapshot = "\n".join([
        f"Now: {datetime.now().strftime('%A %H:%M')}",
        f"Window in front: {active_window() or 'unknown'}",
        f"Minutes since the user last spoke to you: {int((time.time() - last_user) / 60) if last_user else 'never'}",
        f"Files changed recently: {', '.join(_recent_files()) or 'none'}",
        f"Timers/reminders: {json.dumps(list_timers_sync()) if list_timers_sync() else 'none'}",
        f"What you said on your own lately:\n" + ("\n".join(said) or "nothing"),
        f"Past interruptions: {stats['engaged']} engaged, {stats['ignored']} ignored"
        + (" - you interrupt too much, be stricter" if stats["ignored"] > stats["engaged"] * 2 + 2 else ""),
        f"Recent conversation:\n" + "\n".join(f"{m['role']}: {m['content'][:200]}" for m in msgs[-6:]),
        f"Screen text (OCR, may be partial):\n{screen or '(unavailable)'}",
    ])
    from .brain.client import brain

    text, _ = await brain.complete([{"role": "system", "content": HEARTBEAT_SYSTEM},
                                    {"role": "user", "content": snapshot}], max_tokens=120, temperature=0.3)
    return parse_decision(text)


def parse_decision(text: str) -> str | None:
    """Only a well-formed 'speak: true' with a message is ever said out loud - never stray reasoning."""
    from .brain.repair import loads_lenient

    if "{" not in text:
        return None
    try:
        j = loads_lenient(text[text.find("{"):])
    except ValueError:
        return None
    if not isinstance(j, dict) or j.get("speak") is not True:
        return None
    msg = str(j.get("message") or "").strip()
    return msg if len(msg) >= 6 else None


# ------------------------------------------------------------------ daily: briefing + diary
def _due_today(at: str, key: str) -> bool:
    """True once per day, at/after the HH:MM time, if it hasn't run today."""
    if not at:
        return False
    try:
        h, m = (int(x) for x in at.split(":"))
    except ValueError:
        return False
    now = datetime.now()
    if (now.hour, now.minute) < (h, m) or (now.hour * 60 + now.minute) - (h * 60 + m) > 180:
        return False  # only within 3 hours after the set time (no 10 pm "morning" briefing after a late start)
    today = now.strftime("%Y-%m-%d")
    st = _state()
    if st.get(key) == today:
        return False
    _save(**{key: today})
    return True


async def _daily(s) -> None:
    if _due_today(s.briefing_time, "briefing_day"):
        from .hands.briefing import compose, gather

        text = compose(await gather())
        # the briefing is wanted: it ignores the gap/cooldown, only quiet hours and pauses
        st = _state()
        if time.time() >= st.get("quiet_until", 0):
            deliver(text, "briefing")
    if _due_today(s.diary_time, "diary_day"):
        from .hands.briefing import write_diary

        try:
            await write_diary()
        except Exception as e:
            log.info("diary failed: %s", e)


# ------------------------------------------------------------------ loop
async def loop(is_busy) -> None:
    last_heartbeat = time.time()
    while True:
        await asyncio.sleep(TICK)
        s = config.store.load()
        if is_busy():
            continue
        try:
            if s.proactive:
                for check, topic in ((check_battery, "battery"), (check_tomorrow, "tomorrow"),
                                     (check_gaming, "gaming")):
                    msg = check()
                    if msg:
                        deliver(msg, topic)
                        break
                else:
                    msg = await check_rain()
                    if msg:
                        deliver(msg, "rain")
            await _daily(s)
            if s.heartbeat_min and time.time() - last_heartbeat >= s.heartbeat_min * 60:
                last_heartbeat = time.time()
                msg = await heartbeat_once()
                if msg:
                    deliver(msg, "heartbeat")
        except Exception as e:
            log.info("proactive check failed: %s", e)


def status() -> dict[str, Any]:
    st = _state()
    return {"quiet_until": st.get("quiet_until", 0), "last_msg": st.get("last_msg", 0),
            "recent": _entries(24)[-10:], "engagement": _mark_engagement()}

