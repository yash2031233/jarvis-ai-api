"""An overloaded provider: wait for it to come back, then carry on with the same request."""

import asyncio

from jarvis import config
from jarvis.brain import client as bc
from jarvis.brain.client import Brain, BrainError, TurnResult
from jarvis.events import bus


def _fast(monkeypatch):
    """No real waiting in tests: every pause is instant (but still notices a cancel)."""
    async def nap(self, _seconds):
        await asyncio.sleep(0)
    monkeypatch.setattr(bc.ProviderWait, "nap", nap)


def _brain(monkeypatch, fails: int, kind: str = "server"):
    b = Brain()
    calls = []

    async def once(model, *a, **k):
        calls.append(model)
        if len(calls) <= fails:
            raise BrainError("Provider error 503.", kind)
        return TurnResult(text="done")

    monkeypatch.setattr(b, "_stream_once", once)
    monkeypatch.setattr(b, "client", lambda: None)
    return b, calls


def test_keeps_waiting_through_a_long_outage_then_continues(monkeypatch):
    _fast(monkeypatch)
    config.store.update(model="main", fallback_model="", provider_wait_min=30)
    b, calls = _brain(monkeypatch, fails=12)                  # far more than the old 5 tries
    q = bus.subscribe()

    async def go():
        bus.loop = asyncio.get_running_loop()
        return await b.stream_turn([{"role": "user", "content": "hi"}])

    r = asyncio.run(go())
    assert r.text == "done" and len(calls) == 13
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    bus.unsubscribe(q)
    waits = [e for e in seen if e.get("type") == "provider_wait"]
    assert [w["on"] for w in waits] == [True, False]           # said once that it's waiting, once that it's back


def test_no_patience_means_no_waiting(monkeypatch):
    _fast(monkeypatch)
    config.store.update(model="main", fallback_model="", provider_wait_min=0)
    b, calls = _brain(monkeypatch, fails=99)
    try:
        asyncio.run(b.stream_turn([{"role": "user", "content": "hi"}]))
        raise AssertionError("should have given up")
    except BrainError:
        pass
    assert len(calls) == bc.ProviderWait.QUICK + 1             # only the quick retries
    config.store.update(provider_wait_min=30)


def test_stop_ends_the_wait(monkeypatch):
    _fast(monkeypatch)
    config.store.update(model="main", fallback_model="", provider_wait_min=30)
    b, calls = _brain(monkeypatch, fails=10_000)

    async def go():
        cancel = asyncio.Event()

        async def later():
            while len(calls) < 6:
                await asyncio.sleep(0)
            cancel.set()
        asyncio.get_running_loop().create_task(later())
        try:
            await b.stream_turn([{"role": "user", "content": "hi"}], cancel=cancel)
        except BrainError:
            return "stopped"
    assert asyncio.run(go()) == "stopped" and len(calls) < 50


def test_auth_errors_never_wait(monkeypatch):
    _fast(monkeypatch)
    config.store.update(model="main", fallback_model="", provider_wait_min=30)
    b, calls = _brain(monkeypatch, fails=99, kind="auth")
    try:
        asyncio.run(b.stream_turn([{"role": "user", "content": "hi"}]))
    except BrainError as e:
        assert e.kind == "auth"
    assert len(calls) == 1

