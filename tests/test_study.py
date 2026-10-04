import json
import time

from jarvis.hands import study


def _deck(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "DECKS", tmp_path)
    d = {"name": "Bio", "created": time.time(), "cards": [
        {"id": "a", "q": "Q1", "a": "A1", "why": "", "choices": [], "box": 1, "due": 0},
        {"id": "b", "q": "Q2", "a": "A2", "why": "", "choices": [], "box": 1, "due": time.time() + 9999},
    ]}
    (tmp_path / "bio.json").write_text(json.dumps(d))


def test_leitner_grading(tmp_path, monkeypatch):
    _deck(tmp_path, monkeypatch)
    assert [c["id"] for c in study.due(study.load("bio"))] == ["a"]
    r = study.grade("Bio", "a", True)
    assert r["box"] == 2 and r["next_in_days"] == 1 and r["due_left"] == 0
    r = study.grade("Bio", "a", False)
    assert r["box"] == 1 and r["due_left"] == 1                      # a miss comes back today
    for _ in range(6):
        study.grade("Bio", "a", True)
    assert study.load("bio")["cards"][0]["box"] == 5                 # capped


def test_decks_summary_and_fuzzy_load(tmp_path, monkeypatch):
    _deck(tmp_path, monkeypatch)
    assert study.decks()[0] == {"deck": "Bio", "cards": 2, "due": 1, "mastered": 0,
                                "created": time.strftime("%Y-%m-%d")}
    assert study.load("bi")["name"] == "Bio"
