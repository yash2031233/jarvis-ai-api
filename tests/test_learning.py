import asyncio
import json
import time

import pytest

from jarvis import config
from jarvis.agent import learning


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "TRACES", tmp_path / "t.jsonl")
    monkeypatch.setattr(learning, "STATE", tmp_path / "s.json")
    config.store.update(vault_path=str(tmp_path / "vault"), learn=True)
    yield tmp_path
    config.store.update(vault_path="")


def test_reflect_merges_and_writes_principles(iso, monkeypatch):
    import jarvis.brain.client as bc

    learning.record("set volume up a bit", [{"tool": "set_volume", "args": {"change": 10}, "ok": False,
                                             "error": "no effect"}], "Done.")
    learning.record("no it didn't change, set it to 60", [{"tool": "set_volume", "args": {"level": 60}, "ok": True,
                                                         "error": ""}], "Volume set to 60%.")
    replies = iter([
        {"lessons": [{"lesson": "On this PC, set volume with level=, not change=", "evidence": "x"}]},
        {"lessons": [{"lesson": "On this PC set the volume using level= rather than change=", "evidence": "y"},
                     {"lesson": "Keep replies to one sentence for simple commands", "evidence": "z"}]},
    ])

    seen = []

    async def fake(messages, **kw):
        seen.append(messages[-1]["content"])
        return json.dumps(next(replies)), "stop"

    monkeypatch.setattr(bc.brain, "complete", fake)
    assert asyncio.run(learning.reflect(force=True))
    assert "USER NEXT SAID: no it didn't change" in seen[0]                       # the correction is the signal
    learning.record("again", [], "ok")
    asyncio.run(learning.reflect(force=True))
    st = json.loads(learning.STATE.read_text())
    assert len(st["lessons"]) == 2 and st["lessons"][0]["count"] == 2          # similar lessons merged
    ps = learning.principles()
    assert any("level=" in p for p in ps)
    assert "level=" in learning.for_prompt()


def test_hand_edits_to_the_note_win(iso):
    from jarvis.memory import vault

    (vault.root() / f"{learning.NOTE}.md").write_text("# x\n\n- Always answer in British English\n")
    assert learning.principles() == ["Always answer in British English"]
    config.store.update(learn=False)
    assert learning.for_prompt() == ""


def test_waits_until_quiet(iso, monkeypatch):
    import jarvis.brain.client as bc

    learning.record("hi", [], "hello")
    called = []

    async def fake(*a, **k):
        called.append(1)
        return "{}", "stop"

    monkeypatch.setattr(bc.brain, "complete", fake)
    assert asyncio.run(learning.reflect()) == [] and not called
    assert time.time()
