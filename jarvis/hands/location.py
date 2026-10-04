"""The `location` tool: where am I, directions with live ETA and spoken turn-by-turn, nearby places, saved places,
location reminders, today's trail. The data side is jarvis.geo; this draws it on the map and words the results."""

from __future__ import annotations

import time
from datetime import datetime

from .. import geo
from .registry import ToolError, tool

ACTIONS = ("where", "route", "nearby", "search", "save_place", "places", "forget_place", "remind", "reminders",
           "cancel_reminder", "trip", "map", "stop")


def _base() -> dict:
    return {"places": geo.places_list(), "reminders": geo.active_reminders()}


@tool(risk="low", timeout=90,
      tags=["location", "where am i", "directions", "route", "navigate", "navigation", "eta", "how long", "drive to",
            "walk to", "nearby", "near me", "closest", "map", "gps", "address", "places", "remind me when i get",
            "traffic", "how far"],
      examples=["location(action='route', to='Micro Center')", "location(action='where')",
                "location(action='nearby', what='coffee')", "location(action='save_place', name='home')",
                "location(action='remind', to='school', text='hand in the form')"])
def location(action: str, to: str = "", mode: str = "drive", what: str = "", name: str = "", address: str = "",
             text: str = "", on: str = "arrive", radius_m: int = 150, id: str = "", hours: float = 12) -> dict:
    """The user's location from their phone's live GPS (shared with their Jarvis Telegram bot) or this device.
    THE source for where they are, routes, ETAs, navigation and what's nearby - never use a browser or maps website
    for these; they don't know where the user is. action:
    where = where they are now; route = directions + ETA to `to` (a saved place, name or address) by `mode`
    drive | walk | bike - it opens the map and starts spoken turn-by-turn navigation; stop = end navigation;
    nearby = places around them matching `what` (coffee, gas, pharmacy...); search = find a place by name/address;
    save_place = remember `name` (at their current spot, or `address`); places / forget_place;
    remind = location reminder: `text` fires when they arrive at (or leave, `on`=leave) `to`;
    reminders / cancel_reminder (`id`); trip = where they've been in the last `hours`; map = just show the map."""
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        raise ToolError(f"Unknown action '{action}'.", hint="one of: " + ", ".join(ACTIONS))
    try:
        return _run(action, to, mode, what, name, address, text, on, radius_m, id, hours)
    except geo.LocationError as e:
        raise ToolError(str(e)) from e


def _run(action, to, mode, what, name, address, text, on, radius_m, id, hours) -> dict:
    base = _base()

    if action in ("where", "map"):
        p = geo.here(approx_ok=True)
        approx = p.get("approx")
        where = (f"around {p['city']}" if p.get("city") else "unknown") if approx else geo.reverse(p["lat"], p["lon"])
        tr = [] if approx else geo.trail(12)
        geo.show(view="me", me=geo.me_view(p), trail=[[q["lat"], q["lon"]] for q in tr], address=where, **base)
        out = {"address": where, "lat": p["lat"], "lon": p["lon"], "accuracy_m": p.get("acc"),
               "as_of": geo.ago(time.time() - p["t"]),
               "live_sharing": bool(time.time() - p["t"] < geo.FRESH_S and p.get("live"))}
        if approx:
            out["approximate"] = ("City-level guess from the internet connection - for real GPS share live location "
                                  "with the Telegram bot (Settings → Location) or allow location on this device.")
        return out

    if action == "route":
        if not to:
            raise ToolError("route needs `to`.")
        me = geo.here()
        dest = geo.resolve(to)
        r = geo.route(dest, me, mode if mode in geo.ROUTER else "drive")
        geo.show(view="route", me=geo.me_view(me), route={**r, "to": dest}, **base)
        return {"to": dest["name"], "address": dest.get("address"), "mode": r["mode"], "distance": geo.fmt_dist(r["dist_m"]),
                "minutes": round(r["secs"] / 60), "arrive_at": r["eta"], "picked_from": r["source"],
                "directions": [f"{s['text']} ({geo.fmt_dist(s['dist_m'])})" for s in r["steps"][:12]],
                "navigation": "started on the map - Jarvis will speak each turn",
                "use_these": "Final answer from the user's live GPS and a real road router - it is already on their map "
                             "with a live ETA. Don't look it up in a browser or maps site."}

    if action == "stop":
        geo.show(view="me", me=geo.me_view(geo.latest()), **base)
        return {"navigation": "stopped"}

    if action in ("nearby", "search"):
        q = what or to
        if not q:
            raise ToolError(f"{action} needs `what`.")
        me = geo.here(required=action == "nearby", approx_ok=True)
        hits = geo.nearby(q, me) if action == "nearby" else geo.search(q, near=me, limit=8, radius_km=50)
        geo.show(view="nearby", me=geo.me_view(me), nearby={"what": q, "results": hits}, **base)
        return {"what": q, "results": [{**h, "distance": geo.fmt_dist(h["dist_m"])} if "dist_m" in h else h for h in hits],
                "use_these": "Real places near the user's position, already pinned on their map - answer from these."}

    if action == "save_place":
        if not name:
            raise ToolError("save_place needs `name`.")
        if address:
            hit = geo.resolve(address)
            spot = {"lat": hit["lat"], "lon": hit["lon"], "address": hit.get("address", address)}
        else:
            p = geo.here()
            spot = {"lat": p["lat"], "lon": p["lon"], "address": geo.reverse(p["lat"], p["lon"])}
        geo.save_place(name, spot)
        base = _base()
        geo.show(view="places", me=geo.me_view(geo.latest()), focus={"name": name, **spot}, **base)
        return {"saved": name, **spot}

    if action == "places":
        geo.show(view="places", me=geo.me_view(geo.latest()), **base)
        return {"places": base["places"]}

    if action == "forget_place":
        gone = geo.forget_place(name)
        return {"forgot": name if gone else None, "places": [p["name"] for p in geo.places_list()]}

    if action == "remind":
        if not (text and to):
            raise ToolError("remind needs `text` and `to`.")
        spot = geo.resolve(to)
        r = geo.add_reminder(text, spot, "leave" if on == "leave" else "arrive", radius_m)
        base = _base()
        geo.show(view="places", me=geo.me_view(geo.latest()),
                 focus={"name": spot["name"], "lat": spot["lat"], "lon": spot["lon"]}, **base)
        note = "fires (here and on Telegram) when the user's live location crosses into the circle"
        if not geo.latest():
            note += " - it needs live location sharing on to work"
        return {"reminder": r, "note": note}

    if action == "reminders":
        geo.show(view="places", me=geo.me_view(geo.latest()), **base)
        return {"reminders": base["reminders"]}

    if action == "cancel_reminder":
        return {"cancelled": geo.cancel_reminder(id)}

    # trip
    tr = geo.trail(hours)
    if not tr:
        return {"points": 0, "note": "no location history in that window"}
    total = sum(geo.dist_m(a, b) for a, b in zip(tr, tr[1:]) if geo.dist_m(a, b) < 5000)  # skip GPS jumps
    geo.show(view="trip", me=geo.me_view(tr[-1]), trail=[[q["lat"], q["lon"]] for q in tr], **base)
    return {"since": datetime.fromtimestamp(tr[0]["t"]).strftime("%I:%M %p").lstrip("0"), "points": len(tr),
            "distance": geo.fmt_dist(total), "started_at": geo.reverse(tr[0]["lat"], tr[0]["lon"]),
            "now_at": geo.reverse(tr[-1]["lat"], tr[-1]["lon"])}
