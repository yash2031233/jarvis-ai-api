"""Study mode: flashcard / quiz decks made from a topic, notes, a file or a photo - practised on the Study screen
or by voice. Missed cards come back sooner (Leitner boxes: box 1 = again today, then 1, 3, 7, 16 days).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .. import config
from ..events import bus
from .registry import ToolError, tool

DECKS = config.DATA_DIR / "study"
INTERVALS = {1: 0, 2: 1, 3: 3, 4: 7, 5: 16}  # days until due again, by box
_lock = threading.Lock()

MAKE_SYSTEM = """You write study flashcards. Make exactly {n} cards that test real understanding of the material
(definitions, causes, steps, formulas applied, comparisons) - not trivia about wording. Each card:
  "q": a clear question, "a": the short correct answer, "why": one sentence explaining it,
  "choices": 4 options for multiple choice - the correct answer (same text as "a") plus 3 plausible wrong ones.
Use only facts from the material when material is given. Reply with JSON only: {{"cards": [...]}}"""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "deck"


def _path(deck: str) -> Path:
    return DECKS / f"{_slug(deck)}.json"


def load(deck: str) -> dict[str, Any]:
    p = _path(deck)
    if not p.exists():
        names = [x.stem for x in DECKS.glob("*.json")] if DECKS.exists() else []
        from rapidfuzz import fuzz, process

        hit = process.extractOne(_slug(deck), names, scorer=fuzz.WRatio) if names else None
        if not hit or hit[1] < 70:
            raise LookupError(f"No deck called '{deck}'. Decks: {', '.join(names) or 'none yet'}")
        p = DECKS / f"{hit[0]}.json"
    return json.loads(p.read_text("utf-8"))


def _save(d: dict[str, Any]) -> None:
    DECKS.mkdir(parents=True, exist_ok=True)
    tmp = _path(d["name"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), "utf-8")
    tmp.replace(_path(d["name"]))


def due(d: dict[str, Any]) -> list[dict[str, Any]]:
    now = time.time()
    return sorted((c for c in d["cards"] if c.get("due", 0) <= now), key=lambda c: (c.get("box", 1), c.get("due", 0)))


def decks() -> list[dict[str, Any]]:
    out = []
    for p in sorted(DECKS.glob("*.json")) if DECKS.exists() else []:
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        cards = d["cards"]
        out.append({"deck": d["name"], "cards": len(cards), "due": len(due(d)),
                    "mastered": sum(1 for c in cards if c.get("box", 1) >= 4),
                    "created": time.strftime("%Y-%m-%d", time.localtime(d.get("created", 0)))})
    return out


def grade(deck: str, card_id: str, right: bool) -> dict[str, Any]:
    with _lock:
        d = load(deck)
        for c in d["cards"]:
            if c["id"] == card_id:
                c["box"] = min(5, c.get("box", 1) + 1) if right else 1
                c["due"] = time.time() + INTERVALS[c["box"]] * 86400
                c["right" if right else "wrong"] = c.get("right" if right else "wrong", 0) + 1
                c["seen"] = time.time()
                _save(d)
                return {"card": card_id, "box": c["box"], "next_in_days": INTERVALS[c["box"]],
                        "due_left": len(due(d))}
    raise LookupError(f"No card {card_id} in {deck}.")


async def _material(material: str, file: str, image: str) -> str:
    parts = [material.strip()] if material.strip() else []
    if file:
        from .files import read_file

        parts.append(read_file(file, max_chars=20000))
    if image:
        import asyncio

        import cv2

        from ..vision import see

        img = await asyncio.to_thread(cv2.imread, image)
        if img is None:
            raise ToolError(f"Couldn't open the image {image}.")
        parts.append(await see.ask([img], "Transcribe all the text and content of this page/worksheet exactly.",
                                   max_tokens=2500, max_side=1600))
    return "\n\n".join(parts)


@tool(risk="low", timeout=300,
      tags=["study", "flashcards", "quiz", "deck", "test me", "learn", "revise", "exam", "homework", "cards"],
      examples=["study(action='make', deck='Cell biology', topic='mitosis vs meiosis', n=12)",
                "study(action='quiz', deck='Cell biology')",
                "study(action='grade', deck='Cell biology', card_id='a1b2', right=true)"])
async def study(action: str, deck: str = "", topic: str = "", material: str = "", file: str = "",
                image: str = "", n: int = 10, card_id: str = "", right: bool = False) -> dict:
    """Flashcards & quizzes. make = build a deck named `deck` from a `topic`, pasted `material`, a `file`
    (.txt/.md/.docx/.pdf) or a photo (`image` path) - ONE make call per request; then tell the user it's on the
    Study screen or offer to quiz them. list = decks and what's due. quiz = the next due cards of `deck` for quizzing
    by voice/chat: ask ONE question at a time, wait for the answer, say if it's right and why (the card's `why`),
    then grade it (action=grade, card_id, right) before the next. Tutor: on a miss, offer a hint first.
    open = show the deck on the Study screen. delete = remove a deck."""
    a = action.lower().strip()
    try:
        if a == "make":
            if not deck.strip():
                raise ToolError("Give the deck a name in `deck`.")
            src = await _material(material, file, image)
            if not src and not topic.strip():
                raise ToolError("Give a `topic`, `material`, `file` or `image` to make cards from.")
            n = max(3, min(int(n), 40))
            from ..brain.client import brain
            from ..brain.repair import loads_lenient

            user = (f"Topic: {topic}\n" if topic else "") + (f"Material:\n{src[:18000]}" if src else
                                                              "Use your own knowledge of the topic.")
            text, _ = await brain.complete(patience=None, messages=[{"role": "system", "content": MAKE_SYSTEM.format(n=n)},
                                            {"role": "user", "content": user}], max_tokens=min(6000, 350 * n + 400))
            try:
                j = loads_lenient(text[text.find("{"):])
            except ValueError:
                raise ToolError("The model didn't return cards.", hint="Try again, maybe with fewer cards.")
            cards = []
            for c in (j.get("cards") if isinstance(j, dict) else j) or []:
                q, ans = str(c.get("q", "")).strip(), str(c.get("a", "")).strip()
                if not q or not ans:
                    continue
                choices = [str(x) for x in (c.get("choices") or []) if str(x).strip()][:4]
                if ans not in choices:
                    choices = (choices[:3] + [ans]) if choices else []
                cards.append({"id": uuid.uuid4().hex[:6], "q": q, "a": ans, "why": str(c.get("why", "")),
                              "choices": choices, "box": 1, "due": 0})
            if not cards:
                raise ToolError("No usable cards came back.", hint="Try again with a clearer topic or material.")
            with _lock:
                try:
                    d = load(deck)
                    d["cards"] += cards
                except LookupError:
                    d = {"name": deck.strip(), "created": time.time(), "source": topic or (file or image or "notes"),
                         "cards": cards}
                _save(d)
            bus.emit("study", deck=d["name"], action="made", count=len(cards))
            return {"deck": d["name"], "added": len(cards), "total": len(d["cards"]),
                    "sample": [{"q": c["q"], "a": c["a"]} for c in cards[:3]],
                    "shown": "on the Study screen"}
        if a == "list":
            return {"decks": decks() or "no decks yet"}
        if a in ("quiz", "next"):
            d = load(deck)
            cards = due(d)[:5]
            if not cards:
                return {"deck": d["name"], "info": "Nothing due - all caught up!", "cards": []}
            return {"deck": d["name"], "due": len(due(d)),
                    "cards": [{"card_id": c["id"], "q": c["q"], "a": c["a"], "why": c["why"],
                               "choices": c.get("choices", [])} for c in cards]}
        if a == "grade":
            if not card_id:
                raise ToolError("Give `card_id` and `right`.")
            return grade(deck, card_id, right)
        if a in ("open", "show"):
            d = load(deck) if deck else None
            bus.emit("study", deck=d["name"] if d else "", action="open")
            return {"shown": "Study screen", "deck": d["name"] if d else None}
        if a == "delete":
            d = load(deck)
            _path(d["name"]).unlink(missing_ok=True)
            return {"deleted": d["name"]}
        raise ToolError(f"Unknown action '{action}'.", hint="make, list, quiz, grade, open, delete")
    except LookupError as e:
        raise ToolError(str(e), hint="Use action=list to see decks.")
