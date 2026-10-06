"""Hand control on the server: the air mouse and gesture shortcuts (with a fake mouse - nothing really moves)."""

import asyncio

import pytest

from jarvis import config
from jarvis.hands import air


class FakeCtl:
    def __init__(self):
        self.position = (0, 0)
        self.log = []

    def press(self, b):
        self.log.append(("press", b))

    def release(self, b):
        self.log.append(("release", b))

    def click(self, b, n):
        self.log.append(("click", b))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dy))


@pytest.fixture
def fake(monkeypatch):
    m = air._Mouse()
    ctl = FakeCtl()
    m._ctl, m._btn = ctl, type("B", (), {"left": "L", "right": "R"})
    monkeypatch.setattr(m, "_bounds", lambda: (-1920, 0, 4480, 1440))     # two monitors, one left of the main one
    monkeypatch.setattr(air, "mouse", m)
    config.store.update(air_mouse=True)
    yield ctl
    config.store.update(air_mouse=False)


def test_moves_across_every_monitor(fake):
    air.handle_air({"ev": "move", "x": 0.0, "y": 0.0})
    assert fake.position == (-1920, 0)
    air.handle_air({"ev": "move", "x": 1.0, "y": 1.0})
    assert fake.position == (-1920 + 4479, 1439)


def test_pinch_drag_and_right_click(fake):
    air.handle_air({"ev": "down", "button": "left", "x": 0.5, "y": 0.5})
    air.handle_air({"ev": "move", "x": 0.6, "y": 0.5})
    air.handle_air({"ev": "up", "button": "left"})
    air.handle_air({"ev": "click", "button": "right"})
    air.handle_air({"ev": "scroll", "notches": 3})
    assert fake.log == [("press", "L"), ("release", "L"), ("click", "R"), ("scroll", 3)]


def test_nothing_happens_with_the_air_mouse_off_but_a_held_button_is_still_let_go(fake):
    air.handle_air({"ev": "down", "button": "left"})
    config.store.update(air_mouse=False)
    air.handle_air({"ev": "click", "button": "left"})
    air.handle_air({"ev": "release"})                                     # hand control turned off mid-drag
    assert fake.log == [("press", "L"), ("release", "L")]


def test_gestures_have_defaults_and_can_be_changed():
    config.store.update(gestures={})
    assert air.mapping()["thumbs_up"] == "play_pause"
    air.hand_gestures(action="set", gesture="peace", does="open spotify")
    assert air.mapping()["peace"] == "say: open spotify"
    air.hand_gestures(action="set", gesture="thumbs up", does="next_track")
    assert air.mapping()["thumbs_up"] == "next_track"
    air.hand_gestures(action="reset")
    assert air.mapping()["peace"] == "screenshot"


def test_a_gesture_runs_its_action(monkeypatch):
    from jarvis.hands import media

    did = []
    monkeypatch.setattr(media, "media_control", lambda a: did.append(a))
    config.store.update(gestures={"rock": "next_track"})
    asyncio.run(air.handle_gesture("rock"))
    asyncio.run(air.handle_gesture("not_a_pose"))
    config.store.update(gestures={})
    assert did == ["next"]
