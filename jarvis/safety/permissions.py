"""Per-tool permissions (auto / ask / off) + confirmation round-trip with the UI."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .. import config
from ..events import bus

DEFAULT_BY_RISK = {"low": "auto", "medium": "ask", "high": "ask"}


def effective(tool_name: str, risk: str) -> str:
    s = config.store.load()
    if tool_name == "run_command" and not s.shell_enabled:
        return "off"
    return s.tool_permissions.get(tool_name) or DEFAULT_BY_RISK.get(risk, "ask")


def is_enabled(tool_name: str) -> bool:
    from ..hands.registry import registry

    t = registry.get(tool_name)
    return bool(t) and effective(tool_name, t.risk) != "off"


class Confirmations:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def ask(self, tool: str, args: dict[str, Any], summary: str, timeout: float = 120) -> bool:
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        bus.emit("confirm", id=cid, tool=tool, args=args, summary=summary)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(cid, None)
            bus.emit("confirm_closed", id=cid)

    def answer(self, cid: str, approved: bool) -> bool:
        fut = self.pending.get(cid)
        if fut and not fut.done():
            fut.set_result(approved)
            return True
        return False

    def answer_latest(self, approved: bool) -> bool:
        """Voice "yes"/"no" answers the most recent pending confirmation."""
        if not self.pending:
            return False
        cid = list(self.pending)[-1]
        return self.answer(cid, approved)


confirmations = Confirmations()
