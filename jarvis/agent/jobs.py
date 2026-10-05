"""Background jobs: a big task ("research X and summarize it", "make flashcards from all my notes") runs in
its own agent - its own conversation - while the main chat carries on. At most two run at once; the rest queue.
When one finishes, a notification arrives and the result is added to the main conversation so you can ask
about it. Jobs are saved, so the list survives a restart (unfinished ones are marked interrupted).
"""

from __future__ import annotations

import asyncio
import contextvars
import re
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


IN_JOB: contextvars.ContextVar[str] = contextvars.ContextVar("in_job", default="")


def _same(a: str, b: str) -> bool:
    def n(s: str) -> str:
        return re.sub(r"\W+", " ", s.lower()).strip()[:300]
    return n(a) == n(b)


def start(task: str) -> dict[str, Any]:
    global _sem
    if IN_JOB.get():
        # a background job's own agent tried to hand its task off again - that's how one job became twenty
        raise RuntimeError("This IS the background job - do the task yourself instead of starting another job.")
    _load()
    for old in _jobs.values():                    # the same task already queued / running: that's the one
        if old["status"] in ("queued", "running") and _same(old["task"], task):
            return {**_public(old), "already": True}
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
        IN_JOB.set(j["id"])
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
    """Jarvis speaks up on his own: the whole result goes into the running conversation (the main chat knows it,
    so "and what about X?" works next), a short version is said out loud, and the phone gets a notification."""
    from ..memory.store import memory
    from .engine import agent

    result = (j.get("result") or "").strip() or "Done."
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"[*#`>|_]", "", result).replace("\n", " "), maxsplit=2)
    spoken = " ".join(sentences[:2])[:260]
    task = re.sub(r"\s+", " ", j["task"])[:90]
    text = f"**Background job done** - {task}\n\n{result}"
    memory.add_message("assistant", text, {"job": j["id"], "proactive": "job"})
    agent.messages.append({"role": "assistant", "content": text})
    bus.emit("proactive", text=text, topic="job", job=j["id"])
    bus.emit("job", id=j["id"], status="done")
    agent._say(f"Your background job is done. {spoken}")


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
