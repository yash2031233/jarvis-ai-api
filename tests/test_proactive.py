import asyncio
import time
from datetime import datetime, timedelta

import pytest

from jarvis import config, proactive
from jarvis.events import bus


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(proactive, "LOG", tmp_path / "p.jsonl")
    monkeypatch.setattr(proactive, "STATE", tmp_path / "s.json")
    config.store.update(quiet_start="22:30", quiet_end="07:00", proactive_gap_min=20, vault_path=str(tmp_path / "v"))
    monkeypatch.setattr(proactive, "_in_quiet_hours", lambda now: False)
    yield
    config.store.update(vault_path="")


def test_quiet_hours_wrap_midnight():
    import importlib

    real = importlib.reload(proactive)._in_quiet_hours
    assert real(datetime(2026, 1, 1, 23, 0)) and real(datetime(2026, 1, 1, 6, 59))
    assert not real(datetime(2026, 1, 1, 12, 0))


def test_gap_cooldown_and_pause():
    assert proactive.can_speak("rain", 60)
    bus.loop = None
    proactive.deliver("Rain soon.", "rain", speak=False)
    assert not proactive.can_speak("battery", 0)        # one message per 20 min overall
    proactive._save(last_msg=time.time() - 3600)
    assert proactive.can_speak("battery", 0)
    assert not proactive.can_speak("rain", 120)          # same topic still cooling down
    proactive.pause(30)
    assert not proactive.can_speak("battery", 0)
    proactive.pause(0)
    assert proactive.can_speak("battery", 0)


def test_tomorrow_check_finds_tests_in_memory(monkeypatch):
    from jarvis.memory import vault

    tom = datetime.now() + timedelta(days=1)
    vault.add("Algebra", f"Quiz on {tom.strftime('%A')}, chapter 4")
    vault.add("Algebra", "Teacher is Mrs. Patel")

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(datetime.today(), datetime.min.time()).replace(hour=18)

    monkeypatch.setattr(proactive, "datetime", FakeDT)
    msg = proactive.check_tomorrow()
    assert msg and "Algebra" in msg and "Quiz" in msg
    monkeypatch.setattr(proactive, "datetime", type("Morning", (datetime,), {
        "now": classmethod(lambda cls, tz=None: datetime.today().replace(hour=9))}))
    assert proactive.check_tomorrow() is None             # only the evening before


def test_heartbeat_silence_by_default(monkeypatch):
    import jarvis.brain.client as bc

    async def say_nothing(messages, **kw):
        assert "Window in front" in messages[-1]["content"]
        return '{"speak": false}', "stop"

    monkeypatch.setattr(bc.brain, "complete", say_nothing)
    assert asyncio.run(proactive.heartbeat_once()) is None


def test_parse_decision_never_speaks_reasoning():
    assert proactive.parse_decision('{"speak": true, "message": "Your essay is due in 8 minutes."}') == \
        "Your essay is due in 8 minutes."
    assert proactive.parse_decision('{"speak": false}') is None
    assert proactive.parse_decision("Nothing yet - it's not 17:00, I'll re-check later.") is None
    assert proactive.parse_decision('{"speak": true, "message": ""}') is None
