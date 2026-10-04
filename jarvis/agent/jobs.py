"""Background jobs: a big task ("research X and summarize it", "make flashcards from all my notes") runs in
its own agent - its own conversation - while the main chat carries on. At most two run at once; the rest queue.
When one finishes, a notification arrives and the result is added to the main conversation so you can ask
about it. Jobs are saved, so the list survives a restart (unfinished ones are marked interrupted).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from .. import config
from ..events import bus

log = logging.getLogger(__name__)
FILE = config.DATA_DIR / "jobs.json"
MAX_RUNNING = 2
JOB_NOTE = ("\n\n(You are running as a BACKGROUND JOB, not in a live chat: nobody can answer questions. Work through "
            "the whole task with your tools, then reply with the final result - complete, well organized, ready to "
            "read. If something can't be done, say what you did and what's missing.)")

_jobs: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}
_agents: dict[str, Any] = {}
_sem: asyncio.Semaphore | None = None


def _load() -> None:
    if _jobs or not FILE.exists():
        return
    try:
        for j in json.loads(FILE.read_text("utf-8")):
            if j["status"] in ("queued", "running"):
                j["status"], j["error"] = "interrupted", "Jarvis was closed while this was running."
            _jobs[j["id"]] = j
    except Exception:
        pass


def _save() -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    keep = sorted(_jobs.values(), key=lambda j: -j["created"])[:50]
    FILE.write_text(json.dumps(keep, indent=1), "utf-8")


def _public(j: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in j.items() if k != "result"}
    out["result_preview"] = (j.get("result") or "")[:300]
    if j.get("started"):
        out["seconds"] = round((j.get("finished") or time.time()) - j["started"])
    return out


def _update(j: dict[str, Any], **kw: Any) -> None:
    j.update(kw)
    _save()
    bus.emit("job", **_public(j))


def start(task: str) -> dict[str, Any]:
    global _sem
    _load()
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_RUNNING)
    jid = uuid.uuid4().hex[:6]
    j = {"id": jid, "task": task.strip(), "status": "queued", "created": time.time(), "tools": [], "error": ""}
    _jobs[jid] = j
    _update(j)
    _tasks[jid] = asyncio.get_running_loop().create_task(_run(j))
    return _public(j)


async def _run(j: dict[str, Any]) -> None:
    from .engine import Agent

    assert _sem is not None
    async with _sem:
        if j["status"] == "cancelled":
            return
        agent = Agent(job_id=j["id"])
        _agents[j["id"]] = agent
        q = bus.subscribe()

        async def watch_tools() -> None:  # record which tools the job uses, for the Jobs list
            while True:
                ev = await q.get()
                if ev.get("type") == "job_tool" and ev.get("job") == j["id"] and ev.get("kind") == "tool_start":
                    j["tools"].append(ev.get("name"))
                    _update(j)

        watcher = asyncio.create_task(watch_tools())
        _update(j, status="running", started=time.time())
        try:
            result = await agent.handle(j["task"] + JOB_NOTE, source="job")
            if agent.cancel_event.is_set():
                _update(j, status="cancelled", finished=time.time())
                return
            _update(j, status="done", finished=time.time(), result=result)
            _announce(j)
        except asyncio.CancelledError:
            _update(j, status="cancelled", finished=time.time())
        except Exception as e:
            log.exception("job %s failed", j["id"])
            _update(j, status="failed", finished=time.time(), error=str(e)[:500])
            bus.emit("notice", level="error", text=f"Background job failed: {j['task'][:80]}")
        finally:
            watcher.cancel()
            bus.unsubscribe(q)
            _agents.pop(j["id"], None)


def _announce(j: dict[str, Any]) -> None:
    from ..memory.store import memory
    from .engine import agent

    text = f"Background job finished - {j['task'][:120]}:\n\n{j['result']}"
    memory.add_message("assistant", text, {"job": j["id"]})
    agent.messages.append({"role": "assistant", "content": text})  # the main chat knows the result
    bus.emit("job_done", id=j["id"], task=j["task"], result=j["result"])


def cancel(jid: str) -> dict[str, Any]:
    _load()
    j = _jobs.get(jid)
    if not j:
        raise LookupError(f"No job {jid}.")
    if j["status"] in ("queued", "running"):
        a = _agents.get(jid)
        if a:
            a.cancel()
        t = _tasks.get(jid)
        if t and j["status"] == "queued":
            t.cancel()
        _update(j, status="cancelled", finished=time.time())
    return _public(j)


def get(jid: str) -> dict[str, Any]:
    _load()
    j = _jobs.get(jid)
    if not j:
        raise LookupError(f"No job {jid}.")
    return {**_public(j), "result": j.get("result")}


def all_jobs() -> list[dict[str, Any]]:
    _load()
    return [_public(j) for j in sorted(_jobs.values(), key=lambda j: -j["created"])]


def delete(jid: str) -> None:
    _load()
    if jid in _jobs and _jobs[jid]["status"] not in ("queued", "running"):
        _jobs.pop(jid)
        _save()
