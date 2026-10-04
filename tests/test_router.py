import pytest

from jarvis.router.fastpath import route


@pytest.mark.parametrize("text,tool,args", [
    ("what time is it", "system_info", {"what": "time"}),
    ("Jarvis, what's the time?", "system_info", {"what": "time"}),
    ("battery", "system_info", {"what": "battery"}),
    ("open spotify", "open_app", {"name": "spotify"}),
    ("hey jarvis can you open vs code please", "open_app", {"name": "vs code"}),
    ("close discord", "close_app", {"name": "discord"}),
    ("volume 40", "set_volume", {"level": 40}),
    ("set volume to fifty percent", "set_volume", {"level": 50}),
    ("mute", "set_volume", {"mute": True}),
    ("unmute", "set_volume", {"mute": False}),
    ("next song", "media_control", {"action": "next"}),
    ("pause", "media_control", {"action": "play_pause"}),
    ("set a timer for 5 minutes", "set_timer", {"seconds": 300}),
    ("10 second timer", "set_timer", {"seconds": 10}),
    ("remind me to call mom in 20 minutes", "set_reminder", {"text": "call mom", "in_minutes": 20.0}),
    ("open github.com", "open_url", {"url": "github.com"}),
    ("undo that", "undo_last_action", {}),
    ("what is 15% of 2400", "calculate", {"expression": "15% of 2400"}),
])
def test_routes(text, tool, args):
    r = route(text)
    assert r is not None, text
    assert r.tool == tool
    assert r.args == args


@pytest.mark.parametrize("text", [
    "what is the capital of france",
    "open the file I downloaded yesterday",
    "open my resume",
    "close this tab",
    "search for the best gpu and open the first result",
    "write an email to my boss about being late",
    "",
])
def test_falls_through_to_agent(text):
    assert route(text) is None
