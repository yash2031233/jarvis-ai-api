import asyncio
import json
import time

import pytest

from jarvis import config
from jarvis.memory import keeper, vault
from jarvis.memory.store import memory


@pytest.fixture
def tmp_vault(tmp_path):
    config.store.update(vault_path=str(tmp_path / "vault"))
    yield tmp_path / "vault"
    config.store.update(vault_path="")


def test_add_dedupe_recall_replace_remove(tmp_vault):
    assert vault.add("Physics", "Lab report due Oct 12")["added"]
    assert not vault.add("physics", "lab report is due Oct 12")["added"]          # near-duplicate, same note
    vault.add("About me", "Prefers short answers")
    hits = vault.recall("when is the physics lab due")
    assert hits and hits[0]["note"] == "Physics" and "Oct 12" in hits[0]["fact"]
    vault.replace("Physics", "Lab report due", "Lab report due Oct 14")
    assert any("Oct 14" in f for f in vault.facts("Physics")) and not any("Oct 12" in f for f in vault.facts("Physics"))
    vault.remove("Physics", "lab report")
    assert vault.facts("Physics") == []
    log = (tmp_vault / "Memory log.md").read_text("utf-8")
    assert "+ [[Physics]]" in log and "~ [[Physics]]" in log and "- [[Physics]]" in log
    assert "Prefers short answers" in vault.for_prompt()


def test_links_and_titles(tmp_vault):
    vault.link("Robot car", "ESP32")
    assert "[[ESP32]]" in vault.read("robot car")
    vault.link("Robot car", "Battery")
    text = vault.read("Robot car")
    assert text.count("Related:") == 1 and "[[Battery]]" in text
    assert vault.add('Bad/Name:?', "x")["note"] == "BadName"                     # filesystem-safe titles


def test_keeper_files_facts_and_skips_secrets(tmp_vault, monkeypatch):
    import jarvis.brain.client as bc

    memory.add_message("user", "My chemistry test moved to Friday. Also my wifi password is hunter2.")
    memory.add_message("assistant", "Noted.")

    async def fake_complete(messages, **kw):
        assert "chemistry" in messages[-1]["content"].lower()
        return json.dumps({"facts": [
            {"op": "add", "note": "Chemistry", "fact": "Test moved to Friday"},
            {"op": "add", "note": "About me", "fact": "Wifi password is hunter2"},
        ]}), "stop"

    monkeypatch.setattr(bc.brain, "complete", fake_complete)
    vault.save_keeper_state(last_id=0)
    from jarvis.events import bus

    async def go():
        bus.loop = asyncio.get_running_loop()
        return await keeper.run_once(force=True)

    filed = asyncio.run(go())
    assert [f["note"] for f in filed] == ["Chemistry"]
    assert vault.facts("Chemistry")
    assert not any("hunter2" in f for f in vault.facts("About me"))
    assert vault.keeper_state()["last_id"] > 0
    assert asyncio.run(go()) == []                                                 # nothing new since


def test_keeper_waits_for_quiet(tmp_vault, monkeypatch):
    memory.add_message("user", "fresh message")
    vault.save_keeper_state(last_id=0)
    called = []
    import jarvis.brain.client as bc

    async def fake_complete(*a, **k):
        called.append(1)
        return '{"facts": []}', "stop"

    monkeypatch.setattr(bc.brain, "complete", fake_complete)
    assert asyncio.run(keeper.run_once()) == [] and not called                     # not quiet for 10 min yet
    assert time.time()
