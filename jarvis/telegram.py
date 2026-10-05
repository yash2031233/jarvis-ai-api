"""Your own Telegram bot as Jarvis's link to your phone.

Make a bot with @BotFather, paste its token in Settings -> Location, press Pair and send the bot the code shown.
From then on, only that chat is listened to:
  * live location / location pins  -> the GPS for routes, ETAs, "where am I", location reminders (jarvis.geo)
  * text messages                  -> asked to Jarvis like typing in the app; the reply comes back in the chat
  * location reminders             -> sent here when they go off (you're out - that's the point)
The token lives in the OS keychain. Uses long polling, so nothing has to be reachable from the internet.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

TOKEN_KEY = "telegram_bot_token"
API = "https://api.telegram.org/bot{token}/{method}"

state: dict[str, Any] = {"running": False, "bot": None, "error": None, "pair_code": None, "pair_until": 0.0,
                         "last_fix": None}
_task: asyncio.Task | None = None
_seen_live: dict[int, float] = {}


def token() -> str:
    return config.get_secret(TOKEN_KEY)


def chat_id() -> int | None:
    return config.store.load().telegram_chat_id


def _call(method: str, **params) -> dict:
    tok = token()
    if not tok:
        raise RuntimeError("No Telegram bot token set.")
    r = httpx.post(API.format(token=tok, method=method), json=params, timeout=20)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or f"Telegram error {r.status_code}")
    return j["result"]


def _send_file(cid: int, ref: str, caption: str = "") -> bool:
    """One MEDIA reference -> a Telegram photo / video / document."""
    from . import media

    p = media.resolve(ref)
    if p is None:
        return False
    k = media.kind(p)
    method, field = {"image": ("sendPhoto", "photo"), "video": ("sendVideo", "video")}.get(k, ("sendDocument", "document"))
    try:
        r = httpx.post(API.format(token=token(), method=method), data={"chat_id": cid, "caption": caption[:1000]},
                       files={field: (p.name, p.read_bytes())}, timeout=120)
        if not r.json().get("ok") and method == "sendPhoto":    # odd sizes are refused as photos: send as a file
            r = httpx.post(API.format(token=token(), method="sendDocument"), data={"chat_id": cid, "caption": caption[:1000]},
                           files={"document": (p.name, p.read_bytes())}, timeout=120)
        return bool(r.json().get("ok"))
    except Exception as e:
        log.warning("telegram file failed: %s", e)
        return False


def send(text: str, media_refs: list[str] | None = None) -> bool:
    """Message the paired chat, with any MEDIA tags in the text (or media_refs) attached as real photos / files.
    Best effort - False when there's no bot or no paired chat."""
    from . import media

    cid = chat_id()
    if not (cid and token()):
        return False
    clean, refs = media.split(text)
    refs += [r for r in (media_refs or []) if r not in refs]
    try:
        if len(refs) == 1 and len(clean) <= 1000 and _send_file(cid, refs[0], clean):
            return True                                   # one picture: the text goes with it as the caption
        for i in range(0, len(clean), 4000):
            _call("sendMessage", chat_id=cid, text=clean[i:i + 4000])
        for r in refs:
            _send_file(cid, r)
        return True
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False


def new_pair_code() -> str:
    code = f"{random.randint(0, 999999):06d}"
    state.update(pair_code=code, pair_until=time.time() + 600)
    return code


def status() -> dict[str, Any]:
    return {"token_set": bool(token()), "running": state["running"], "bot": state["bot"], "error": state["error"],
            "paired": bool(chat_id()), "pair_code": state["pair_code"] if time.time() < state["pair_until"] else None,
            "last_fix": state["last_fix"]}


# ------------------------------------------------------------------ the poller
async def _handle(upd: dict[str, Any], client: httpx.AsyncClient, tok: str) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    cid = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    async def reply(t: str) -> None:
        for i in range(0, len(t), 4000):
            await client.post(API.format(token=tok, method="sendMessage"), json={"chat_id": cid, "text": t[i:i + 4000]})

    paired = chat_id()
    if cid != paired:
        # only the pairing message is accepted from an unknown chat
        code = state["pair_code"]
        words = text.split()
        if code and time.time() < state["pair_until"] and code in words:
            config.store.update(telegram_chat_id=cid)
            state.update(pair_code=None)
            await reply("Paired with Jarvis ✓\nShare your live location with me (📎 → Location → Share My Live "
                        "Location) and I'll use it for directions, ETAs and location reminders. You can also just "
                        "message me here.")
            from .events import bus

            bus.emit("notice", level="info", text="Telegram paired ✓")
        elif text.startswith("/start"):
            await reply("Hi! To link me to your Jarvis, open Jarvis → Settings → Location → Pair, and send me the "
                        "6-digit code it shows.")
        return

    venue = msg.get("venue")
    loc = (venue or {}).get("location") or msg.get("location")
    if loc:
        live = loc.get("live_period") is not None
        t = msg.get("edit_date") or msg.get("date")
        from . import geo

        await asyncio.to_thread(geo.record, loc["latitude"], loc["longitude"], loc.get("horizontal_accuracy"),
                                loc.get("heading"), live, float(t) if t else None, "phone")
        state["last_fix"] = time.time()
        mid = msg.get("message_id", 0)
        if "edited_message" in upd or mid in _seen_live:
            return
        if live:
            _seen_live[mid] = time.time()
            await reply("Got your live location 📍 I'll use it for directions, ETAs and reminders.")
        else:
            where = (venue or {}).get("title") or await asyncio.to_thread(geo.reverse, loc["latitude"], loc["longitude"])
            await reply(f"Got it - you're at {where}. Ask me for directions, what's nearby, or to save it as a place.")
        return

    if text and "edited_message" not in upd:
        if text in ("/start", "/help"):
            await reply("Message me like you'd talk to Jarvis. Share your live location for directions and ETAs.")
            return
        from .agent.engine import agent

        await client.post(API.format(token=tok, method="sendChatAction"), json={"chat_id": cid, "action": "typing"})
        answer = await agent.handle(text, source="telegram")
        from . import media

        clean, refs = media.split(answer or "")
        if refs:
            await asyncio.to_thread(send, answer)          # with its pictures / files attached
        else:
            await reply(clean or "Done.")


async def _loop() -> None:
    offset = 0
    backoff = 2.0
    while True:
        tok = token()
        if not tok:
            state.update(running=False)
            return
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                if not state["bot"]:
                    me = (await client.get(API.format(token=tok, method="getMe"))).json()
                    if not me.get("ok"):
                        raise RuntimeError(me.get("description") or "bad token")
                    state["bot"] = "@" + me["result"]["username"]
                state.update(running=True, error=None)
                r = await client.post(API.format(token=tok, method="getUpdates"),
                                      json={"offset": offset, "timeout": 30,
                                            "allowed_updates": ["message", "edited_message"]})
                j = r.json()
                if not j.get("ok"):
                    raise RuntimeError(j.get("description") or "getUpdates failed")
                for upd in j["result"]:
                    offset = upd["update_id"] + 1
                    try:
                        await _handle(upd, client, tok)
                    except Exception:
                        log.exception("telegram update failed")
                backoff = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.update(error=str(e)[:200])
            if "Unauthorized" in str(e) or "bad token" in str(e) or "Not Found" in str(e):
                state.update(running=False, bot=None)
                log.warning("telegram bot token rejected: %s", e)
                return
            log.info("telegram poll error (%s); retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


def start() -> None:
    """(Re)start the poller - call after the token changes. Needs the server's event loop."""
    global _task
    if os.environ.get("JARVIS_NO_TELEGRAM"):     # a second copy (testing) must not take the bot's messages
        return
    if _task and not _task.done():
        _task.cancel()
    state.update(bot=None, error=None, running=False)
    if token():
        _task = asyncio.get_running_loop().create_task(_loop())
