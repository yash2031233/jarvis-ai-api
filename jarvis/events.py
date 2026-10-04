"""Tiny async pub/sub bus. Everything the UI shows flows through here."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def emit(self, type_: str, **data: Any) -> None:
        """Safe to call from any thread."""
        msg = {"type": type_, **data}
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(msg)
        else:
            loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.debug("dropping event for slow subscriber")


bus = EventBus()


def orb_state(state: str, **extra: Any) -> None:
    """idle | listening | thinking | speaking | working | error"""
    bus.emit("orb", state=state, **extra)
