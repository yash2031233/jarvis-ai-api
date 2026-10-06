from datetime import datetime, timedelta

import pytest

from jarvis.hands import briefing
from jarvis.hands.registry import ToolError


def test_compose_mentions_what_matters():
    text = briefing.compose({"date": "Monday, October 5", "weather": {"now": 15.2, "high": 18, "low": 9,
                             "conditions": "overcast", "rain_chance": 70, "units": "C", "place": "X"},
                             "today": ["Algebra: Quiz today"], "reminders": [], "todo": ["Buy filament"], "jobs_done": []})
    assert "Monday, October 5" in text and "70% chance of rain" in text and "Quiz today" in text
    assert "1 item" in text
    empty = briefing.compose({"date": "Monday", "today": [], "reminders": [], "todo": [], "jobs_done": []})
    assert "Nothing on the calendar" in empty


def test_parse_day():
    now = datetime.now()
    assert briefing._parse_day("yesterday").date() == (now - timedelta(days=1)).date()
    assert briefing._parse_day("2026-10-01").date().isoformat() == "2026-10-01"
    d = briefing._parse_day("last monday")
    assert d.weekday() == 0 and 1 <= (now.date() - d.date()).days <= 7
    assert briefing._parse_day("3 days ago").date() == (now - timedelta(days=3)).date()
    with pytest.raises(ToolError):
        briefing._parse_day("the day the music died")


def test_daily_runs_once_in_window(tmp_path, monkeypatch):
    from jarvis import proactive

    monkeypatch.setattr(proactive, "STATE", tmp_path / "s.json")
    t = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
    if datetime.now().hour == 0 and datetime.now().minute < 5:
        pytest.skip("too close to midnight")
    assert proactive._due_today(t, "k") is True
    assert proactive._due_today(t, "k") is False               # once a day
    late = (datetime.now() - timedelta(hours=4)).strftime("%H:%M")
    assert proactive._due_today(late, "k2") is False           # not hours later
    assert proactive._due_today("", "k3") is False             # off
