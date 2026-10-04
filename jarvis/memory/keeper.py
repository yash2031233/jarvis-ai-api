"""Memory keeper: files what's worth remembering into the vault by itself - in the background, never mid-chat.

About 10 minutes after the last message (and only when Jarvis isn't busy), it reads the conversation since its
last pass and asks the model once for the lasting facts: plans, tests and deadlines, people, preferences, things
bought/finished/changed. Each goes into the right note (or replaces the line it updates); every change is written
to the "Memory log" note so it can be checked.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import config
from ..brain.repair import loads_lenient
from ..events import bus
from . import vault
from .store import memory

log = logging.getLogger(__name__)
QUIET_MIN = 10 * 60
CHECK_EVERY = 120

SYSTEM = """You maintain a person's long-term memory: a vault of small notes, one per thing (a person, a class, a
project, a device, a place, "About me" for the user's own facts and preferences).
From the conversation, extract only LASTING facts worth remembering weeks from now: plans and dates, tests and
deadlines, people and relationships, preferences, things bought/finished/changed, projects and their state.
Skip small talk, one-off questions, anything the assistant said about itself, and NEVER store passwords, keys or
codes. Prefer existing note titles. If a fact updates an older one (a new date, a changed plan), use "replace".
Reply with JSON only: {"facts": [{"op": "add"|"replace", "note": "Note title", "fact": "short specific fact",
"old": "text of the line it replaces (replace only)"}]} - or {"facts": []} if nothing is worth keeping."""


async def run_once(force: bool = False) -> list[dict]:
    state = vault.keeper_state()
    last_id = int(state.get("last_id", 0))
    msgs = memory.messages_since(last_id, limit=200)
    if not msgs:
        return []
    if not force and time.time() - msgs[-1]["ts"] < QUIET_MIN:
        return []
    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in msgs if m["content"])
    name = config.store.load().user_name or "the user"
    prompt = (f"The user is {name}. Existing notes: {', '.join(vault.notes()[:120]) or '(none yet)'}\n\n"
              f"Conversation:\n{convo[-16000:]}")
    from ..brain.client import brain

    text, _ = await brain.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                   max_tokens=900, temperature=0.1)
    try:
        j = loads_lenient(text[text.find("{"):] if "{" in text else text)
    except ValueError:
        log.info("memory keeper: unreadable reply")
        vault.save_keeper_state(last_id=msgs[-1]["id"], last_run=time.time())
        return []
    applied = []
    for f in (j.get("facts") if isinstance(j, dict) else []) or []:
        try:
            note, fact = str(f.get("note", "")).strip() or "Inbox", str(f.get("fact", "")).strip()
            if not fact or any(w in fact.lower() for w in ("password", "api key", "passcode", "pin code")):
                continue
            if f.get("op") == "replace" and f.get("old"):
                r = vault.replace(note, str(f["old"]), fact, source="memory keeper")
            else:
                r = vault.add(note, fact, source="memory keeper")
            if r.get("added") or r.get("replaced"):
                applied.append({"note": r["note"], "fact": fact})
        except Exception as e:
            log.debug("keeper skip: %s", e)
    vault.save_keeper_state(last_id=msgs[-1]["id"], last_run=time.time())
    if applied:
        bus.emit("notice", level="info", text=f"Remembered {len(applied)} thing(s) from our conversation.")
    log.info("memory keeper filed %d facts", len(applied))
    return applied


async def loop(is_busy) -> None:
    while True:
        await asyncio.sleep(CHECK_EVERY)
        if not config.store.load().memory_keeper or is_busy():
            continue
        try:
            await run_once()
        except Exception as e:
            log.info("memory keeper failed: %s", e)
        if config.store.load().learn and not is_busy():
            try:
                from ..agent import learning

                await learning.reflect()
            except Exception as e:
                log.info("reflection failed: %s", e)


def status() -> dict:
    s = vault.keeper_state()
    return {"last_run": s.get("last_run"), "last_id": s.get("last_id", 0)}

