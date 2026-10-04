"""Phone notifications (Web Push) for the installed phone app - they arrive even when the app is closed.

Reminders and timers going off, background jobs finishing, location reminders and Jarvis speaking up on his own are
sent to every phone that turned notifications on (Settings → Phone notifications, on the phone). The signing key is
made once and kept in the keychain; nothing goes through any server but the phone maker's push service.
iPhone: needs iOS 16.4+ and the app added to the home screen.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from typing import Any

from . import config
from .events import bus

log = logging.getLogger(__name__)

SUBS = config.DATA_DIR / "push_subscriptions.json"
KEY_NAME = "vapid_private"
CLAIMS = {"sub": "https://github.com/yash2031233/jarvis-ai-api"}
_lock = threading.Lock()
_vapid = None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def vapid():
    """The signing key: made once, then kept in the keychain."""
    global _vapid
    if _vapid is not None:
        return _vapid
    from py_vapid import Vapid01

    pem = config.get_secret(KEY_NAME)
    if pem:
        _vapid = Vapid01.from_pem(pem.encode())
    else:
        v = Vapid01()
        v.generate_keys()
        config.set_secret(KEY_NAME, v.private_pem().decode())
        _vapid = v
    return _vapid


def public_key() -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return _b64(vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def _subs() -> list[dict]:
    try:
        return json.loads(SUBS.read_text("utf-8"))
    except Exception:
        return []


def _save(subs: list[dict]) -> None:
    SUBS.parent.mkdir(parents=True, exist_ok=True)
    SUBS.write_text(json.dumps(subs, indent=1), "utf-8")


def subscribe(sub: dict) -> int:
    with _lock:
        subs = [s for s in _subs() if s.get("endpoint") != sub.get("endpoint")] + [sub]
        _save(subs)
        return len(subs)


def count() -> int:
    return len(_subs())


def send(title: str, body: str, url: str = "/", tag: str = "jarvis") -> int:
    """Notify every subscribed phone. Phones that unsubscribed (or uninstalled the app) are forgotten."""
    from pywebpush import WebPushException, webpush

    subs = _subs()
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body[:300], "url": url, "tag": tag})
    ok, gone = 0, []
    for s in subs:
        try:
            webpush(s, payload, vapid_private_key=vapid(), vapid_claims=dict(CLAIMS), ttl=3600, timeout=10)
            ok += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                gone.append(s.get("endpoint"))
            else:
                log.warning("push failed (%s): %s", code, e)
        except Exception as e:
            log.warning("push failed: %s", e)
    if gone:
        with _lock:
            _save([s for s in _subs() if s.get("endpoint") not in gone])
    return ok


def _note(ev: dict[str, Any]) -> tuple[str, str] | None:
    t = ev.get("type")
    if t == "alert":
        return "Jarvis", ev.get("text", "")
    if t == "job_done":
        return "Background job finished", ev.get("task", "")[:120]
    if t == "proactive":
        return "Jarvis", ev.get("text", "")
    return None


async def loop() -> None:
    """Forward the things worth a notification to the phones."""
    q = bus.subscribe()
    try:
        while True:
            ev = await q.get()
            n = _note(ev)
            if n and n[1] and _subs():
                await asyncio.to_thread(send, *n)
    finally:
        bus.unsubscribe(q)
