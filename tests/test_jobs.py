import asyncio

from jarvis.agent import jobs
from jarvis.brain.client import TurnResult
from jarvis.brain.repair import ToolCall
from jarvis.events import bus


class SlowBrain:
    def __init__(self):
        self.calls = 0

    async def stream_turn(self, messages, tools=None, on_text=None, on_tool_call=None, cancel=None, **kw):
        self.calls += 1
        await asyncio.sleep(0.05)
        if self.calls == 1:
            return TurnResult(tool_calls=[ToolCall("calculate", {"expression": "6*7"})])
        if on_text:
            on_text("The answer is 42.")
        return TurnResult(text="The answer is 42.")


def test_job_runs_in_background_and_announces(monkeypatch, tmp_path):
    import jarvis.agent.engine as eng
    from jarvis.hands import load_builtin_tools

    load_builtin_tools()
    monkeypatch.setattr(eng, "brain", SlowBrain())
    monkeypatch.setattr(jobs, "FILE", tmp_path / "jobs.json")
    jobs._jobs.clear()
    jobs._sem = None

    async def go():
        bus.loop = asyncio.get_running_loop()
        q = bus.subscribe()
        j = jobs.start("Work out six times seven and report back")
        assert j["status"] == "queued"
        for _ in range(100):
            await asyncio.sleep(0.05)
            if jobs.get(j["id"])["status"] == "done":
                break
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return j["id"], events

    jid, events = asyncio.run(go())
    done = jobs.get(jid)
    assert done["status"] == "done" and "42" in done["result"]
    assert "calculate" in done["tools"]
    types = {e["type"] for e in events}
    assert "job_done" in types and "job_tool" in types
    assert "delta" not in types and "reply" not in types        # nothing leaked into the main chat stream
    assert eng.agent.messages[-1]["content"].startswith("Background job finished")


def test_cancel_queued(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "FILE", tmp_path / "jobs.json")
    jobs._jobs.clear()
    jobs._sem = None

    async def go():
        bus.loop = asyncio.get_running_loop()
        jobs._sem = asyncio.Semaphore(0)                      # nothing can start
        j = jobs.start("A long research task that will wait in the queue")
        await asyncio.sleep(0.01)
        return jobs.cancel(j["id"])

    assert asyncio.run(go())["status"] == "cancelled"
