"""Learning from experience - without retraining the model.

1. trace     every request is recorded: what was asked, which tools ran (and failed), the reply, and what the user
             said next (a correction like "no, I meant..." is the strongest signal)
2. reflect   when things go quiet, new traces are reviewed for lessons: mistakes and how they were fixed, how
             the user wants things done, tool quirks on this machine
3. principles lessons are merged (similar ones reinforce each other) into a short list that goes into every system
             prompt. It lives as the "Jarvis principles" note in the vault, so it can be read or edited by hand.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from rapidfuzz import fuzz

from .. import config
from ..brain.repair import loads_lenient

log = logging.getLogger(__name__)
TRACES = config.DATA_DIR / "traces.jsonl"
STATE = config.DATA_DIR / "learning_state.json"
NOTE = "Jarvis principles"
MAX_PRINCIPLES = 15

REFLECT_SYSTEM = """You review an AI desktop assistant's recent work to find LESSONS that will make it better next
time. You get requests with the tools it called (failures marked), its reply, and what the user said next.
Look for: a tool call that failed and what finally worked; the user correcting or redoing something ("no, I meant",
"that's wrong", repeating the request); how the user wants things done (format, length, which app/account/folder);
quirks of this computer. Ignore one-off facts about the world (memory handles those) and anything already working.
Each lesson must be general, actionable and short ("When X, do Y"). Most sessions teach nothing - that's fine.
Reply with JSON only: {"lessons": [{"lesson": "...", "evidence": "the request it came from"}]} or {"lessons": []}"""


# ------------------------------------------------------------------ 1. traces
def record(request: str, steps: list[dict[str, Any]], reply: str) -> None:
    TRACES.parent.mkdir(parents=True, exist_ok=True)
    with TRACES.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "request": request[:1000], "steps": steps[-20:],
                            "reply": reply[:800]}) + "\n")


def _traces_since(t: float) -> list[dict[str, Any]]:
    if not TRACES.exists():
        return []
    out = []
    for ln in TRACES.read_text("utf-8").splitlines():
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e["t"] > t:
            out.append(e)
    return out


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {"last": 0.0, "lessons": []}


def _save_state(s: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1), "utf-8")


# ------------------------------------------------------------------ 2. reflect
async def reflect(force: bool = False) -> list[str]:
    st = _state()
    traces = _traces_since(st.get("last", 0.0))
    if not traces:
        return []
    if not force and time.time() - traces[-1]["t"] < 10 * 60:   # wait until things are quiet
        return []
    # the user's next request is feedback on the previous answer
    lines = []
    for i, tr in enumerate(traces[-30:]):
        steps = "; ".join(f"{s['tool']}({json.dumps(s.get('args', {}))[:120]})"
                          f"{' FAILED: ' + s['error'][:120] if not s.get('ok') else ''}" for s in tr["steps"])
        nxt = traces[-30:][i + 1]["request"] if i + 1 < len(traces[-30:]) else "(nothing)"
        lines.append(f"REQUEST: {tr['request']}\nTOOLS: {steps or 'none'}\nREPLY: {tr['reply'][:300]}\n"
                     f"USER NEXT SAID: {nxt[:300]}")
    from ..brain.client import brain

    text, _ = await brain.complete([{"role": "system", "content": REFLECT_SYSTEM},
                                    {"role": "user", "content": "\n\n".join(lines)[-14000:]}],
                                   max_tokens=700, temperature=0.2)
    try:
        j = loads_lenient(text[text.find("{"):] if "{" in text else "{}")
    except ValueError:
        j = {}
    new = [str(x.get("lesson", "")).strip() for x in (j.get("lessons") or []) if isinstance(x, dict)]
    new = [n for n in new if 12 <= len(n) <= 300]
    st["last"] = traces[-1]["t"]
    _merge(st, new)
    _save_state(st)
    _write_note(st)
    if new:
        log.info("learned: %s", new)
    return new


def _key_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_=]+", s.lower()) if len(w) > 3 or "=" in w}


def similar(a: str, b: str) -> float:
    """0..1: shared key words (catches rewordings) or near-identical text."""
    wa, wb = _key_words(a), _key_words(b)
    overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
    return max(overlap, fuzz.token_set_ratio(a.lower(), b.lower()) / 100 - 0.05)


def _merge(st: dict[str, Any], new: list[str]) -> None:
    """Similar lessons reinforce each other (count) instead of piling up; the list stays short."""
    lessons = st.setdefault("lessons", [])
    for n in new:
        best = max(lessons, key=lambda l: similar(l["text"], n), default=None)
        if best and similar(best["text"], n) >= 0.72:
            best["count"] += 1
            best["last"] = time.time()
            if len(n) < len(best["text"]):
                best["text"] = n  # keep the crisper wording
        else:
            lessons.append({"text": n, "count": 1, "first": time.time(), "last": time.time()})
    # rank by evidence and recency; drop the weakest beyond the cap
    lessons.sort(key=lambda l: (l["count"], l["last"]), reverse=True)
    del lessons[MAX_PRINCIPLES:]


# ------------------------------------------------------------------ 3. principles
def _write_note(st: dict[str, Any]) -> None:
    from ..memory import vault

    p = vault.root() / f"{NOTE}.md"
    body = "\n".join(f"- {l['text']}  _(seen {l['count']}x)_" for l in st.get("lessons", []))
    p.write_text(f"# {NOTE}\n\nWhat Jarvis has learned from experience. Edit or delete lines freely - "
                 f"they go into every conversation.\n\n{body}\n", "utf-8")


def principles() -> list[str]:
    """From the note if it exists (so hand edits win), else from state."""
    from ..memory import vault

    p = vault.root() / f"{NOTE}.md"
    if p.exists():
        out = []
        for ln in p.read_text("utf-8").splitlines():
            m = re.match(r"^\s*[-*]\s+(.*?)(\s+_\(seen \d+x\)_)?\s*$", ln)
            if m and m.group(1).strip():
                out.append(m.group(1).strip())
        return out[:MAX_PRINCIPLES]
    return [l["text"] for l in _state().get("lessons", [])][:MAX_PRINCIPLES]


def for_prompt() -> str:
    if not config.store.load().learn:
        return ""
    ps = principles()
    return "\n".join(f"- {p}" for p in ps)
