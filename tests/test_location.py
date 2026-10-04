"""Location / navigation data side (no network: the map services are faked)."""

import asyncio

import pytest

from jarvis import geo
from jarvis.events import bus
from jarvis.hands import load_builtin_tools, registry

load_builtin_tools()


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "DATA", tmp_path)
    monkeypatch.setattr(geo, "TRACK", tmp_path / "track.jsonl")
    monkeypatch.setattr(geo, "PLACES", tmp_path / "places.json")
    monkeypatch.setattr(geo, "REMINDERS", tmp_path / "reminders.json")
    monkeypatch.setattr(geo, "reverse", lambda lat, lon: "1 Test Street, Testville")
    monkeypatch.setattr(geo, "_announce", lambda r: None)
    geo.MAP_STATE.clear()


def run(tool_name, **args):
    async def go():
        bus.loop = asyncio.get_running_loop()
        return await registry.run(tool_name, args)
    return asyncio.run(go())


def test_polyline_decoding():
    # the example from Google's polyline format documentation
    pts = geo._poly("_p~iF~ps|U_ulLnnqC_mqNvxq`@", 5)
    assert pts == [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]


def test_step_wording():
    assert geo._step_text({"maneuver": {"type": "turn", "modifier": "right"}, "name": "Route 9"}) == "Turn right onto Route 9"
    assert geo._step_text({"maneuver": {"type": "arrive", "modifier": "left"}}) == "Arrive - destination on the left"
    assert geo._step_text({"maneuver": {"type": "roundabout", "exit": 2}, "name": "Main St"}) == \
        "At the roundabout take exit 2 onto Main St"


def test_no_fix_says_how_to_share():
    r = run("location", action="route", to="anywhere")
    assert not r.ok and "Telegram" in r.error


def test_reminder_fires_on_arrival_once():
    geo.record(40.0, -74.0)                                     # far away
    r = geo.add_reminder("buy milk", {"name": "shop", "lat": 40.01, "lon": -74.0})
    geo.record(40.0, -74.0)                                     # first fix after creating it: learns the side
    assert geo.record(40.0005, -74.0) == []                     # still outside (~1 km away)
    fired = geo.record(40.01, -74.0)                            # arrives
    assert [f["id"] for f in fired] == [r["id"]]
    assert geo.record(40.0, -74.0) == [] and geo.active_reminders() == []


def test_route_tool_shows_map_and_reports_eta(monkeypatch):
    geo.record(40.7580, -73.9855, 10)
    shown = {}
    monkeypatch.setattr(geo, "show", lambda **p: (shown.clear(), shown.update(p)))
    monkeypatch.setattr(geo, "search", lambda q, near=None, **k: [
        {"name": "Empire State Building", "address": "350 5th Ave", "lat": 40.7484, "lon": -73.9857}])
    fake = {"src": "osrm", "secs": 900, "dist_m": 1300, "geometry": [[40.758, -73.9855], [40.7484, -73.9857]],
            "steps": [{"text": "Head out on 7th Avenue", "dist_m": 600, "at": [40.758, -73.9855]},
                      {"text": "Arrive - destination on the right", "dist_m": 0, "at": [40.7484, -73.9857]}]}
    monkeypatch.setattr(geo, "_osrm", lambda d, o, m: [dict(fake)])
    monkeypatch.setattr(geo, "_valhalla", lambda *a, **k: [])
    monkeypatch.setattr(geo, "_gkey", lambda: None)
    r = run("location", action="route", to="empire state building", mode="walk")
    assert r.ok, r.error
    assert r.output["minutes"] == 15 and r.output["to"] == "Empire State Building"
    assert shown["view"] == "route" and shown["route"]["to"]["name"] == "Empire State Building" and not shown.get("routing")
    assert shown["route"]["mode"] == "walk" and len(shown["route"]["geometry"]) == 2


def test_saved_places_resolve_by_name(monkeypatch):
    geo.record(51.5, -0.12)
    monkeypatch.setattr(geo, "show", lambda **p: None)
    assert run("location", action="save_place", name="Home").ok
    assert geo.resolve("home")["lat"] == 51.5
    assert run("location", action="forget_place", name="home").output["forgot"] == "home"


def test_weather_uses_live_location():
    from jarvis.hands import weather

    geo.record(48.85, 2.35)
    lat, lon, place = asyncio.run(weather._locate(""))
    assert (lat, lon, place) == (48.85, 2.35, "your location")


def test_telegram_bot_and_pairing_survive_a_restart(monkeypatch):
    import keyring

    from jarvis import config, telegram

    vault = {}
    monkeypatch.setattr(keyring, "set_password", lambda svc, k, v: vault.__setitem__((svc, k), v))
    monkeypatch.setattr(keyring, "get_password", lambda svc, k: vault.get((svc, k)))
    monkeypatch.setattr(keyring, "delete_password", lambda svc, k: vault.pop((svc, k), None))
    config.set_secret(telegram.TOKEN_KEY, "123456789:AAtest-token-abcdefghijklmnop")
    config.store.update(telegram_chat_id=4242)
    monkeypatch.setattr(config, "store", config._Store())            # a fresh process reads it all back from disk
    st = telegram.status()
    assert st["token_set"] and st["paired"] and telegram.chat_id() == 4242
    config.store.update(telegram_chat_id=None)
    config.set_secret(telegram.TOKEN_KEY, "")


def test_telegram_pairing_and_locations():
    from jarvis import config, telegram

    sent = []

    class FakeClient:
        async def post(self, url, json=None):
            sent.append(json)

    config.store.update(telegram_chat_id=None)
    code = telegram.new_pair_code()
    asyncio.run(telegram._handle({"message": {"chat": {"id": 99}, "text": "nope"}}, FakeClient(), "t"))
    assert config.store.load().telegram_chat_id is None             # strangers can't pair without the code
    asyncio.run(telegram._handle({"message": {"chat": {"id": 42}, "text": code}}, FakeClient(), "t"))
    assert config.store.load().telegram_chat_id == 42 and "Paired" in sent[-1]["text"]

    async def live():
        bus.loop = asyncio.get_running_loop()
        await telegram._handle({"message": {"chat": {"id": 42}, "message_id": 7,
                                            "location": {"latitude": 1.5, "longitude": 2.5, "live_period": 900}}},
                               FakeClient(), "t")
        await telegram._handle({"edited_message": {"chat": {"id": 42}, "message_id": 7, "edit_date": 1,
                                                   "location": {"latitude": 1.6, "longitude": 2.6, "live_period": 900}}},
                               FakeClient(), "t")
        await telegram._handle({"message": {"chat": {"id": 99}, "location": {"latitude": 9, "longitude": 9}}},
                               FakeClient(), "t")
    asyncio.run(live())
    p = geo.latest()
    assert (p["lat"], p["lon"], p["live"]) == (1.6, 2.6, True)       # the stranger's pin was ignored
    config.store.update(telegram_chat_id=None)
