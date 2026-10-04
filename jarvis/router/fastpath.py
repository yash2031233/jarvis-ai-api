"""Fast-path intent router: common commands execute instantly with NO LLM call.

Patterns are anchored to the whole utterance so anything longer or more nuanced
falls through to the agent. Target: < 150 ms from text to action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

WORD_NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
            "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
            "a": 1, "an": 1, "half an": 0.5, "a half": 0.5}

PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay|yo)\s+)?(?:jarvis[,!.]?\s*)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?", re.I)
SUFFIX = re.compile(r"(?:[,\s]+(?:please|now|for me|thanks|thank you|jarvis))*[.!?\s]*$", re.I)


def normalize(text: str) -> str:
    t = text.strip()
    t = PREFIX.sub("", t)
    t = SUFFIX.sub("", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def num(s: str) -> float:
    s = s.strip().lower()
    try:
        return float(s)
    except ValueError:
        return float(WORD_NUM.get(s, -1))


def _dur(amount: str, unit: str) -> int:
    n = num(amount)
    u = unit.lower()
    mult = 3600 if u.startswith("h") else 60 if u.startswith("m") else 1
    return int(n * mult)


@dataclass
class Route:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    say: str | None = None  # if None, the tool's output is spoken


@dataclass
class Rule:
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str]], Route | None]


N = r"(\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|a|an|half an)"
UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
# Things that should NOT be treated as app names by "open X"
NOT_APPS = re.compile(r"(\.|/|https?:|www|file|folder|document|the |my |a |an |that|this|it$|tab)", re.I)

RULES: list[Rule] = [
    # time / date
    Rule(re.compile(r"^(?:what(?:'s| is) the )?time(?: is it)?(?: now)?$|^what time is it$"),
         lambda m: Route("system_info", {"what": "time"})),
    Rule(re.compile(r"^(?:what(?:'s| is) )?(?:the |today'?s )?date(?: today)?$|^what day is (?:it|today)$"),
         lambda m: Route("system_info", {"what": "date"})),
    # battery / system
    Rule(re.compile(r"^(?:what(?:'s| is) (?:my |the )?)?battery(?: level| status| percentage)?$|^how much battery(?: do i have| is left)?$"),
         lambda m: Route("system_info", {"what": "battery"})),
    Rule(re.compile(r"^(?:what(?:'s| is) (?:my |the )?)?(?:cpu|processor)(?: usage| load)?$"),
         lambda m: Route("system_info", {"what": "cpu"})),
    Rule(re.compile(r"^(?:what(?:'s| is) (?:my |the )?)?(?:ram|memory)(?: usage)?$"),
         lambda m: Route("system_info", {"what": "memory"})),
    # media
    Rule(re.compile(r"^(?:pause|resume|play)(?: (?:the )?(?:music|song|track|video|media|playback))?$"),
         lambda m: Route("media_control", {"action": "play_pause"}, say="")),
    Rule(re.compile(r"^(?:next|skip)(?: (?:song|track|this))?$|^skip (?:this )?(?:song|track)$"),
         lambda m: Route("media_control", {"action": "next"}, say="")),
    Rule(re.compile(r"^(?:previous|last|go back)(?: (?:song|track))?$"),
         lambda m: Route("media_control", {"action": "previous"}, say="")),
    # volume
    Rule(re.compile(rf"^(?:set (?:the )?)?volume (?:to |at )?{N}(?: ?%| percent)?$"),
         lambda m: Route("set_volume", {"level": int(num(m.group(1)))})),
    Rule(re.compile(r"^(?:turn (?:the )?volume up|volume up|louder|turn it up)$"),
         lambda m: Route("set_volume", {"change": 10}, say="")),
    Rule(re.compile(r"^(?:turn (?:the )?volume down|volume down|quieter|turn it down)$"),
         lambda m: Route("set_volume", {"change": -10}, say="")),
    Rule(re.compile(r"^(?:mute|unmute)(?: (?:the )?(?:sound|audio|volume))?$"),
         lambda m: Route("set_volume", {"mute": not m.group(0).startswith("unmute")}, say="")),
    # timers
    Rule(re.compile(rf"^(?:set (?:a )?)?timer (?:for )?{N} ?{UNIT}$|^{N} ?{UNIT} timer$"),
         lambda m: Route("set_timer", {"seconds": _dur(m.group(1) or m.group(3), m.group(2) or m.group(4))})),
    Rule(re.compile(rf"^remind me (?:to )?(.+?) in {N} ?{UNIT}$"),
         lambda m: Route("set_reminder", {"text": m.group(1), "in_minutes": _dur(m.group(2), m.group(3)) / 60})),
    Rule(re.compile(rf"^remind me in {N} ?{UNIT} (?:to )?(.+)$"),
         lambda m: Route("set_reminder", {"text": m.group(3), "in_minutes": _dur(m.group(1), m.group(2)) / 60})),
    Rule(re.compile(r"^(?:cancel|stop) (?:the )?(?:timer|reminder|alarm)$"),
         lambda m: Route("cancel_timer", {})),
    # open / close apps
    Rule(re.compile(r"^(?:open|launch|start|run) (?:up )?([a-z0-9][\w .+&'-]{0,40})$"),
         lambda m: None if NOT_APPS.search(m.group(1)) else Route("open_app", {"name": m.group(1)})),
    Rule(re.compile(r"^(?:close|quit|exit|kill) ([a-z0-9][\w .+&'-]{0,40})$"),
         lambda m: None if NOT_APPS.search(m.group(1)) else Route("close_app", {"name": m.group(1)})),
    Rule(re.compile(r"^(?:switch to|go to|focus|bring up) ([a-z0-9][\w .+&'-]{0,40})$"),
         lambda m: None if NOT_APPS.search(m.group(1)) else Route("focus_app", {"name": m.group(1)})),
    # websites
    Rule(re.compile(r"^(?:open|go to|visit) ((?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?)$"),
         lambda m: Route("open_url", {"url": m.group(1)})),
    # undo / dark mode / lock
    Rule(re.compile(r"^undo(?: that| the last (?:thing|action|change))?$"),
         lambda m: Route("undo_last_action", {})),
    Rule(re.compile(r"^(?:turn on |enable |switch to )?dark mode(?: on)?$"),
         lambda m: Route("set_dark_mode", {"enabled": True})),
    Rule(re.compile(r"^(?:turn on |enable |switch to )?light mode(?: on)?$|^(?:turn off|disable) dark mode$"),
         lambda m: Route("set_dark_mode", {"enabled": False})),
    Rule(re.compile(r"^lock (?:the |my )?(?:screen|computer|pc)$"),
         lambda m: Route("power_action", {"action": "lock"})),
    # quick math
    Rule(re.compile(r"^(?:what(?:'s| is) |calculate )?([\d\s.+\-*/^()%x×÷]+(?:of [\d.]+)?)$"),
         lambda m: Route("calculate", {"expression": m.group(1).replace("x", "*")})
         if re.search(r"\d\s*[-+*/^x×÷%]\s*\d|% of", m.group(1)) else None),
]


def route(text: str) -> Route | None:
    t = normalize(text)
    if not t or len(t) > 80:
        return None
    for r in RULES:
        m = r.pattern.match(t)
        if m:
            res = r.build(m)
            if res is not None:
                return res
    return None


def speak_result(tool: str, args: dict[str, Any], output: Any) -> str:
    """Short natural phrasing for fast-path results (no LLM)."""
    if tool == "system_info" and isinstance(output, dict):
        if "time" in output and args.get("what") == "time":
            return f"It's {output['time']}."
        if args.get("what") == "date":
            return f"Today is {output['date']}."
        if "battery" in output:
            b = output["battery"]
            if isinstance(b, dict):
                tail = ", charging" if b["plugged_in"] else (
                    f", about {b['minutes_left'] // 60}h {b['minutes_left'] % 60}m left" if b["minutes_left"] else "")
                return f"Battery is at {b['percent']} percent{tail}."
            return "This machine doesn't have a battery."
        if "cpu_percent" in output:
            return f"CPU is at {output['cpu_percent']:.0f} percent."
        if "ram" in output:
            r = output["ram"]
            return f"Using {r['used_gb']} of {r['total_gb']} gigabytes of memory."
    if isinstance(output, str):
        return output
    return "Done."
