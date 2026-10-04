"""Location: where the user is, routes with ETAs, what's nearby, named places, location reminders.

Where the position comes from (newest fix wins):
  phone      the user's phone shares live location with their own Telegram bot (Settings -> Location); every update
             lands here - the same way Jarvis v1 did it, and it works away from home
  this device  the app window's own GPS / Wi-Fi location (laptops), sent while the map is open or navigating
  approximate  IP geolocation - city-level, only for "where am I"/"nearby" when there's nothing better; never routes

Maps are OpenStreetMap: Nominatim (search), Overpass (nearby), OSRM + Valhalla (routing, re-timed on one yardstick so
the fastest real route wins), OSM tiles. No keys. With a Google Maps key (Settings -> Location) routes come from
Google's live-traffic router instead. The location history never leaves this computer.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import config
from .events import bus

log = logging.getLogger(__name__)

DATA = config.DATA_DIR / "location"
TRACK = DATA / "track.jsonl"
PLACES = DATA / "places.json"
REMINDERS = DATA / "reminders.json"
UA = {"User-Agent": "JarvisAIAPI/1.0 (open-source desktop assistant; low volume)"}
NOMINATIM = "https://nominatim.openstreetmap.org"
ROUTER = {"drive": "https://routing.openstreetmap.de/routed-car/route/v1/driving",
          "walk": "https://routing.openstreetmap.de/routed-foot/route/v1/driving",
          "bike": "https://routing.openstreetmap.de/routed-bike/route/v1/driving"}
FRESH_S = 15 * 60          # a fix older than this is "last known", not "now"
GOOGLE_KEY = "google_maps_key"
_lock = threading.Lock()          # the track + reminders files
_nom_lock = threading.Lock()      # Nominatim's one-request-a-second rule
_nom_last = [0.0]


class LocationError(Exception):
    pass


def _http() -> httpx.Client:
    return httpx.Client(headers=UA, timeout=20, follow_redirects=True)


# ---- storage -------------------------------------------------------------------------------------

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def latest() -> dict | None:
    if not TRACK.exists():
        return None
    with TRACK.open("rb") as f:          # read just the tail - the track grows all day
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4096))
        lines = f.read().decode("utf-8", "replace").strip().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def trail(hours: float = 12) -> list[dict]:
    if not TRACK.exists():
        return []
    cutoff = time.time() - hours * 3600
    out = []
    with TRACK.open(encoding="utf-8") as f:
        for line in f:
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                continue
            if p.get("t", 0) >= cutoff:
                out.append(p)
    return out


def dist_m(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def record(lat: float, lon: float, acc: float | None = None, heading: float | None = None,
           live: bool = False, t: float | None = None, source: str = "phone") -> list[dict]:
    """Store one fix, tell the map, and return any reminders it set off."""
    point = {"lat": round(lat, 6), "lon": round(lon, 6), "acc": acc, "heading": heading, "live": live,
             "t": t or time.time(), "src": source}
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        with TRACK.open("a", encoding="utf-8") as f:
            f.write(json.dumps(point) + "\n")
        fired = _check_reminders(point)
    bus.emit("geo", **point)
    return fired


# ---- reminders ----------------------------------------------------------------------------------

def _check_reminders(point: dict) -> list[dict]:
    rems = _read_json(REMINDERS, [])
    fired, changed = [], False
    for r in rems:
        if r.get("done"):
            continue
        inside = dist_m(point, r) <= r.get("radius_m", 150)
        was = r.get("inside")
        if was is None:                       # first fix after creating it: just learn which side we're on
            r["inside"] = inside
            changed = True
            continue
        if inside != was:
            r["inside"] = inside
            changed = True
            if (inside and r.get("on", "arrive") == "arrive") or (not inside and r.get("on") == "leave"):
                r["done"] = True
                r["fired_at"] = time.time()
                fired.append(r)
    if changed:
        _write_json(REMINDERS, rems)
    for r in fired:
        threading.Thread(target=_announce, args=(r,), daemon=True).start()
    return fired


def _announce(r: dict) -> None:
    """A reminder went off: on the phone (Telegram - you're out, that's the point) and on the screen."""
    verb = "You're at" if r.get("on", "arrive") == "arrive" else "You just left"
    text = f"📍 {verb} {r.get('place') or 'the spot'}: {r['text']}"
    try:
        from . import telegram

        telegram.send(text)
    except Exception:
        pass
    bus.emit("geo", reminder_fired=r, text=text)
    bus.emit("alert", text=text)


# ---- the outside world (OpenStreetMap) ----------------------------------------------------------

def _nominatim(path: str, **params) -> Any:
    with _nom_lock:
        wait = 1.05 - (time.time() - _nom_last[0])
        if wait > 0:
            time.sleep(wait)
        _nom_last[0] = time.time()
    with _http() as c:
        r = c.get(f"{NOMINATIM}/{path}", params={"format": "jsonv2", **params}, timeout=15)
    r.raise_for_status()
    return r.json()


def _short_address(a: dict) -> str:
    ad = a.get("address") or {}
    street = " ".join(x for x in (ad.get("house_number"), ad.get("road")) if x)
    town = ad.get("city") or ad.get("town") or ad.get("village") or ad.get("hamlet") or ad.get("suburb") or ""
    name = a.get("name") or ""
    bits = [b for b in (name if name and name not in street else "", street, town) if b]
    return ", ".join(bits) or a.get("display_name", "")[:80]


def reverse(lat: float, lon: float) -> str:
    try:
        return _short_address(_nominatim("reverse", lat=lat, lon=lon, zoom=18, addressdetails=1))
    except Exception:
        return f"{lat:.5f}, {lon:.5f}"


def search(q: str, near: dict | None = None, limit: int = 5, radius_km: float = 25) -> list[dict]:
    params: dict[str, Any] = {"q": q, "limit": limit, "addressdetails": 1}
    if near:
        d = radius_km / 111
        params.update(viewbox=f"{near['lon'] - d},{near['lat'] + d},{near['lon'] + d},{near['lat'] - d}", bounded=1)
    res = _nominatim("search", **params)
    if not res and near:                        # nothing close by: widen to anywhere
        res = _nominatim("search", q=q, limit=limit, addressdetails=1)
    out = []
    for a in res:
        p = {"name": a.get("name") or q, "address": _short_address(a), "lat": float(a["lat"]), "lon": float(a["lon"])}
        if near:
            p["dist_m"] = round(dist_m(near, p))
        out.append(p)
    if near:
        out.sort(key=lambda p: p["dist_m"])
    return out


OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
# everyday words -> OpenStreetMap tags; anything not here is matched against place names instead
CATEGORIES = {
    ("coffee", "cafe", "café", "starbucks", "tea"): '["amenity"="cafe"]',
    ("gas", "fuel", "petrol", "gas station"): '["amenity"="fuel"]',
    ("pharmacy", "drugstore", "cvs", "walgreens"): '["amenity"="pharmacy"]',
    ("food", "restaurant", "eat", "dinner", "lunch"): '["amenity"~"^(restaurant|fast_food)$"]',
    ("fast food", "burger", "mcdonalds"): '["amenity"="fast_food"]',
    ("grocery", "groceries", "supermarket"): '["shop"="supermarket"]',
    ("convenience", "7-eleven", "wawa"): '["shop"="convenience"]',
    ("hospital", "er", "emergency room"): '["amenity"="hospital"]',
    ("urgent care", "clinic", "doctor"): '["amenity"~"^(clinic|doctors)$"]',
    ("atm",): '["amenity"="atm"]', ("bank",): '["amenity"="bank"]',
    ("parking",): '["amenity"="parking"]', ("library",): '["amenity"="library"]',
    ("gym", "fitness"): '["leisure"="fitness_centre"]', ("park", "parks"): '["leisure"="park"]',
    ("electronics", "micro center", "best buy"): '["shop"~"^(electronics|computer)$"]',
    ("hardware", "home depot", "lowes"): '["shop"~"^(hardware|doityourself)$"]',
    ("ice cream", "dessert"): '["amenity"="ice_cream"]',
}


def _overpass(q: str) -> list[dict]:
    """Public Overpass servers get busy; try the next one before giving up."""
    err = None
    with _http() as c:
        for url in OVERPASS:
            try:
                r = c.post(url, data={"data": q}, timeout=25)
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:
                err = e
    raise LocationError(f"map search is busy right now ({err})")


def nearby(what: str, me: dict, radius_m: int = 5000, limit: int = 8) -> list[dict]:
    """What's around - by category when the word is a known kind of place, else by name."""
    w = what.strip().lower()
    tag = next((t for words, t in CATEGORIES.items() if w in words or w.rstrip("s") in words), None)
    name = what.replace('"', '').replace(chr(92), '')   # it goes inside an Overpass string literal
    out: list[dict] = []
    for radius in (radius_m, radius_m * 2):
        around = f'nwr(around:{radius},{me["lat"]},{me["lon"]})'
        if tag:
            body = f"{around}{tag};"
        else:  # by name or cuisine ("pizza"). Area first, text second: a bare name regex scans the whole planet.
            body = (f'({around}["amenity"];{around}["shop"];)->.a;'
                    f'(nwr.a["name"~"{name}",i];nwr.a["cuisine"~"{name}",i];);')
        try:
            elements = _overpass(f"[out:json][timeout:20];{body}out center tags 60;")
        except LocationError:
            if out or radius != radius_m:
                break                          # the wider look timed out: keep what the close one found
            raise
        out = []
        for e in elements:
            t, c = e.get("tags", {}), (e.get("center") or e)
            if "lat" not in c or not t.get("name"):
                continue
            street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
            p = {"name": t["name"], "address": ", ".join(x for x in (street, t.get("addr:city")) if x),
                 "lat": c["lat"], "lon": c["lon"], "hours": t.get("opening_hours")}
            p["dist_m"] = round(dist_m(me, p))
            out.append(p)
        if out:                                # something close is better than more, later
            break
    out.sort(key=lambda p: p["dist_m"])
    return out[:limit]


# ---- where the user is ---------------------------------------------------------------------------

NO_FIX = ("I don't know where you are yet. Either share your live location with your Jarvis Telegram bot "
          "(Settings → Location; on the phone: 📎 → Location → Share My Live Location), or open the map on this "
          "device and allow location access.")


def ip_locate() -> dict | None:
    """City-level position from the internet connection. Several free services - they come and go."""
    tries = [("https://ipwho.is/", lambda j: j.get("success") and (j["latitude"], j["longitude"], j.get("city"))),
             ("https://ipapi.co/json/", lambda j: "latitude" in j and (j["latitude"], j["longitude"], j.get("city"))),
             ("http://ip-api.com/json/", lambda j: j.get("status") == "success" and (j["lat"], j["lon"], j.get("city"))),
             ("https://ipinfo.io/json", lambda j: "loc" in j and (*map(float, j["loc"].split(",")), j.get("city")))]
    with _http() as c:
        for url, pick in tries:
            try:
                got = pick(c.get(url, timeout=6).json())
            except Exception:
                continue
            if got:
                return {"lat": float(got[0]), "lon": float(got[1]), "city": got[2] or "", "approx": True,
                        "acc": 5000, "t": time.time(), "src": "ip"}
    return None


def here(required: bool = True, approx_ok: bool = False) -> dict | None:
    """The newest real fix; with approx_ok an IP guess when there's none at all."""
    p = latest()
    if p:
        return p
    if approx_ok:
        p = ip_locate()
        if p:
            return p
    if required:
        raise LocationError(NO_FIX)
    return None


def resolve(place: str) -> dict:
    """'home' / a saved place / 'here' / an address or name -> {name, lat, lon, address}."""
    key = (place or "").strip().lower()
    if key in ("here", "me", "current", "my location", "where i am"):
        p = here()
        return {"name": "your location", "lat": p["lat"], "lon": p["lon"], "address": reverse(p["lat"], p["lon"])}
    saved = _read_json(PLACES, {})
    for name, p in saved.items():
        if name.lower() == key:
            return {"name": name, **p}
    hits = search(place, near=here(required=False, approx_ok=True))
    if not hits:
        raise LocationError(f"Couldn't find {place!r}.")
    return hits[0]


STEP_VERB = {"turn": "Turn", "new name": "Continue", "continue": "Continue", "merge": "Merge", "on ramp": "Take the ramp",
             "off ramp": "Take the exit", "fork": "Keep", "end of road": "Turn", "roundabout": "At the roundabout",
             "rotary": "At the roundabout", "roundabout turn": "At the roundabout", "exit roundabout": "Exit the roundabout"}


def _step_text(s: dict) -> str:
    m = s.get("maneuver", {})
    kind, mod, road = m.get("type", ""), (m.get("modifier") or "").replace("uturn", "U-turn"), s.get("name") or ""
    if kind == "depart":
        return f"Head out{' on ' + road if road else ''}"
    if kind == "arrive":
        return "Arrive" + (f" - destination on the {mod}" if mod in ("left", "right") else "")
    if kind in ("roundabout", "rotary"):
        ex = m.get("exit")
        return (f"At the roundabout take exit {ex}" + (f" onto {road}" if road else "")) if ex else "Go through the roundabout"
    verb = STEP_VERB.get(kind, "Continue")
    return f"{verb}{' ' + mod if mod and kind != 'new name' else ''}{' onto ' + road if road else ''}".strip()


# ---- routing -----------------------------------------------------------------------------------
# One OSRM answer isn't enough: OSRM knows no traffic and times local roads optimistically, so it often takes a
# back-road short cut that's slower in real life. So:
#   * with a Google Maps key: Google's own traffic-aware routes, alternatives included, fastest wins.
#   * without one: every candidate from OSRM (alternatives) and Valhalla (alternates) is re-timed on ONE yardstick
#     (Valhalla, which charges for turns, lights and junctions the way real driving does) and the fastest wins.
VALHALLA = "https://valhalla1.openstreetmap.de/route"
V_COSTING = {"drive": "auto", "walk": "pedestrian", "bike": "bicycle"}
G_MODE = {"drive": "DRIVE", "walk": "WALK", "bike": "BICYCLE"}


def _gkey() -> str | None:
    return config.get_secret(GOOGLE_KEY) or None


def _poly(enc: str, prec: int) -> list:
    """Decode an encoded polyline (Google: 5 digits, Valhalla: 6) -> [[lat, lon], ...]."""
    out, i, lat, lon, f = [], 0, 0, 0, 10 ** prec
    while i < len(enc):
        vals = []
        for _ in range(2):
            shift = res = 0
            while True:
                c = ord(enc[i]) - 63
                i += 1
                res |= (c & 0x1F) << shift
                shift += 5
                if c < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append([lat / f, lon / f])
    return out


def _osrm(dest: dict, origin: dict, mode: str) -> list:
    url = f"{ROUTER[mode]}/{origin['lon']},{origin['lat']};{dest['lon']},{dest['lat']}"
    with _http() as c:
        r = c.get(url, params={"overview": "full", "geometries": "geojson", "steps": "true", "alternatives": "3"})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "Ok" or not d.get("routes"):
        return []
    return [{"src": "osrm", "secs": rt["duration"], "dist_m": rt["distance"],
             "geometry": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]],
             "steps": [{"text": _step_text(s_), "dist_m": round(s_.get("distance", 0)),
                        "at": list(reversed(s_.get("maneuver", {}).get("location", [0, 0])))}
                       for leg in rt["legs"] for s_ in leg["steps"]]} for rt in d["routes"]]


def _v_trip(t: dict) -> dict:
    shape = [pt for leg in t["legs"] for pt in _poly(leg["shape"], 6)]
    steps = []
    for leg in t["legs"]:
        pts = _poly(leg["shape"], 6)
        for m in leg["maneuvers"]:
            kind, txt = m.get("type", 0), (m.get("instruction") or "").strip().rstrip(".")
            if kind == 27:                             # "exit the roundabout": the entry step already said which exit
                continue
            if kind in (1, 2, 3):
                road = (m.get("street_names") or [""])[0]
                txt = f"Head out{' on ' + road if road else ''}"
            elif kind in (4, 5, 6):
                txt = "Arrive" + (" - destination on the right" if kind == 5 else " - destination on the left" if kind == 6 else "")
            # "onto Main Street/CR 12. Continue on CR 12" -> "onto Main Street" (it's read aloud)
            txt = re.sub(r"(?:/[^/]*?)+(?= toward | onto | for |,|$)", "", txt.split(". ")[0]).replace(" (3)", "").strip()
            at = pts[min(m.get("begin_shape_index", 0), len(pts) - 1)]
            steps.append({"text": txt, "dist_m": round(m.get("length", 0) * 1000), "at": at})
    return {"src": "valhalla", "secs": t["summary"]["time"], "dist_m": t["summary"]["length"] * 1000,
            "geometry": shape, "steps": steps}


def _valhalla(locs: list, mode: str, alternates: int = 0) -> list:
    q: dict[str, Any] = {"locations": locs, "costing": V_COSTING[mode], "units": "kilometers",
                         "directions_options": {"units": "kilometers"}}
    if alternates:
        q["alternates"] = alternates
    with _http() as c:
        r = c.post(VALHALLA, json=q)
    if r.status_code != 200:
        return []
    d = r.json()
    trips = [d["trip"]] + [a["trip"] for a in d.get("alternates", [])]
    return [_v_trip(t) for t in trips if t.get("status") == 0 or "legs" in t]


def _retime(c: dict, mode: str) -> float | None:
    """Valhalla's time for driving exactly this candidate's line (pinned by 'through' points along it)."""
    g = c["geometry"]
    cum = [0.0]
    for i in range(1, len(g)):
        cum.append(cum[-1] + dist_m({"lat": g[i - 1][0], "lon": g[i - 1][1]}, {"lat": g[i][0], "lon": g[i][1]}))
    n, total, pts, j = 9, cum[-1] or 1, [], 0          # the public server takes at most 10 points
    for k in range(n + 1):
        want = total * k / n
        while j < len(cum) - 1 and cum[j] < want:
            j += 1
        pts.append(g[j])
    locs = [{"lat": pt[0], "lon": pt[1], "type": "break" if k in (0, n) else "through", "radius": 15}
            for k, pt in enumerate(pts)]
    try:
        v = _valhalla(locs, mode)
        if not v:
            return None
        # a through point that snapped onto some other road shows up as a detour: don't trust that timing
        return v[0]["secs"] if v[0]["dist_m"] < c["dist_m"] * 1.08 else None
    except Exception:
        return None


def _google(dest: dict, origin: dict, mode: str, key: str) -> list:
    body: dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin["lat"], "longitude": origin["lon"]}}},
        "destination": {"location": {"latLng": {"latitude": dest["lat"], "longitude": dest["lon"]}}},
        "travelMode": G_MODE[mode], "computeAlternativeRoutes": True, "languageCode": "en-US", "units": "IMPERIAL"}
    if mode == "drive":
        body["routingPreference"] = "TRAFFIC_AWARE"
    with _http() as c:
        r = c.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body, headers={
            "X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,"
            "routes.legs.steps.navigationInstruction,routes.legs.steps.distanceMeters,routes.legs.steps.startLocation"})
    if r.status_code != 200:
        raise LocationError(f"Google routes {r.status_code}: {r.text[:160]}")
    out = []
    for rt in r.json().get("routes", []):
        steps = [{"text": "Head out"}]
        for leg in rt.get("legs", []):
            for st in leg.get("steps", []):
                ins = ((st.get("navigationInstruction") or {}).get("instructions") or "Continue").split("\n")[0].strip().rstrip(".")
                ll = (st.get("startLocation") or {}).get("latLng") or {}
                steps.append({"text": ins, "dist_m": st.get("distanceMeters", 0), "at": [ll.get("latitude", 0), ll.get("longitude", 0)]})
        steps = steps[1:]
        if steps:
            steps[0]["text"] = "Head out" if not steps[0]["text"].lower().startswith(("head", "turn", "continue")) else steps[0]["text"]
            if steps[0]["text"].lower().startswith("head"):
                steps[0]["text"] = "Head out" + (" on " + steps[0]["text"].split(" on ", 1)[1] if " on " in steps[0]["text"] else "")
        g = _poly(rt["polyline"]["encodedPolyline"], 5)
        steps.append({"text": "Arrive", "dist_m": 0, "at": g[-1]})
        out.append({"src": "google", "secs": float(str(rt.get("duration", "0s")).rstrip("s")),
                    "dist_m": rt.get("distanceMeters", 0), "geometry": g, "steps": steps})
    return out


def route(dest: dict, origin: dict, mode: str = "drive", thorough: bool = True) -> dict:
    """The fastest route: Google's live-traffic one when there's a key, else the best of OSRM + Valhalla."""
    if mode not in ROUTER:
        raise LocationError("mode must be drive, walk or bike")
    cands: list[dict] = []
    note = ""
    key = _gkey()
    if key:
        try:
            cands = _google(dest, origin, mode, key)
            note = "Google Maps, live traffic"
        except Exception as e:
            note = f"Google failed ({str(e)[:60]}), open routers"
    if not cands:
        ends = [{"lat": origin["lat"], "lon": origin["lon"], "type": "break"},
                {"lat": dest["lat"], "lon": dest["lon"], "type": "break"}]
        with ThreadPoolExecutor(2) as ex:
            fo, fv = ex.submit(_osrm, dest, origin, mode), ex.submit(_valhalla, ends, mode, 2)
            try:
                osrm = fo.result()
            except Exception:
                osrm = []
            try:
                val = fv.result()
            except Exception:
                val = []
        for c in val:
            c["score"] = c["secs"]
        if val and osrm and thorough:
            with ThreadPoolExecutor(4) as ex:
                times = list(ex.map(lambda c: _retime(c, mode), osrm))
            for c, t in zip(osrm, times):
                if t:
                    c["score"] = t
        cands = val + [c for c in osrm if "score" in c] if val else osrm
        note = "best of OSRM + Valhalla" if val and osrm else "Valhalla" if val else "OSRM"
    if not cands:
        raise LocationError(f"No {mode} route found.")
    best = min(cands, key=lambda c: c.get("score", c["secs"]))
    secs = best.get("score", best["secs"])
    return {"mode": mode, "dist_m": round(best["dist_m"]), "secs": round(secs),
            "source": note + f" ({best['src']}, {len(cands)} compared)",
            "eta": (datetime.now() + timedelta(seconds=secs)).strftime("%I:%M %p").lstrip("0"),
            "geometry": best["geometry"], "steps": best["steps"][:80], "live_traffic": best["src"] == "google",
            "compared": sorted(round(c.get("score", c["secs"]) / 60, 1) for c in cands)}


# ---- what the map shows ----------------------------------------------------------------------------
MAP_STATE: dict[str, Any] = {}


def show(**payload) -> None:
    """Draw on the map (view: me | route | nearby | places | trip, plus the data for it) and open it."""
    MAP_STATE.clear()
    MAP_STATE.update(payload)
    bus.emit("map", **payload)


def me_view(p: dict | None) -> dict | None:
    if not p:
        return None
    return {**p, "age_s": round(time.time() - p["t"]), "fresh": time.time() - p["t"] < FRESH_S}


def places_list() -> list[dict]:
    return [{"name": k, **v} for k, v in _read_json(PLACES, {}).items()]


def active_reminders() -> list[dict]:
    return [r for r in _read_json(REMINDERS, []) if not r.get("done")]


def ago(s: float) -> str:
    return "just now" if s < 90 else f"{round(s / 60)} min ago" if s < 5400 else f"{round(s / 3600, 1)} h ago"


def fmt_dist(m: float) -> str:
    return f"{round(m)} m" if m < 950 else f"{m / 1000:.1f} km" + (f" ({m / 1609.34:.1f} mi)" if m > 1500 else "")


def save_place(name: str, spot: dict) -> None:
    places = _read_json(PLACES, {})
    places[name] = spot
    _write_json(PLACES, places)


def forget_place(name: str) -> bool:
    places = _read_json(PLACES, {})
    key = next((k for k in places if k.lower() == name.strip().lower()), None)
    if key:
        places.pop(key)
        _write_json(PLACES, places)
    return key is not None


def add_reminder(text: str, spot: dict, on: str = "arrive", radius_m: int = 150) -> dict:
    r = {"id": uuid.uuid4().hex[:6], "text": text, "place": spot["name"], "lat": spot["lat"], "lon": spot["lon"],
         "radius_m": max(40, radius_m), "on": on, "created": time.time()}
    with _lock:
        allr = _read_json(REMINDERS, [])
        allr.append(r)
        _write_json(REMINDERS, allr)
    return r


def cancel_reminder(rid: str) -> int:
    with _lock:
        allr = _read_json(REMINDERS, [])
        keep = [r for r in allr if r["id"] != rid]
        _write_json(REMINDERS, keep)
    return len(allr) - len(keep)
