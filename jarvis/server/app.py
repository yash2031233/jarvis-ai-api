"""Local server: serves the orb UI and bridges it to the agent + voice over a WebSocket.

Binds to 127.0.0.1 only. A per-launch token protects the API from other local web pages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, config
from ..agent.engine import agent
from ..agent import skills
from ..brain.client import BrainError, brain
from ..events import bus
from ..hands import load_builtin_tools, load_plugins, registry
from ..hands import apps as apps_mod
from ..hands import files as files_mod
from ..hardware import detect, voice_profile
from ..memory.store import memory
from ..safety import permissions
from ..safety.undo import journal

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
PLUGIN_DIRS = [Path(__file__).resolve().parents[2] / "plugins", config.DATA_DIR / "plugins"]
TOKEN = secrets.token_urlsafe(24)

state: dict[str, Any] = {"voice": None, "plugins": [], "started": time.time()}
metrics: dict[str, list[int]] = defaultdict(list)


async def _metrics_collector() -> None:
    q = bus.subscribe()
    while True:
        ev = await q.get()
        if ev["type"] == "tool_end" and ev.get("ms") is not None:
            m = metrics[ev["name"]]
            m.append(ev["ms"])
            del m[:-50]
        elif ev["type"] == "done":
            m = metrics["__request__"]
            m.append(ev["ms"])
            del m[:-50]


def start_voice() -> None:
    s = config.store.load()
    if not s.voice_enabled or state["voice"] is not None or os.environ.get("JARVIS_NO_VOICE"):
        return
    try:
        from ..voice.pipeline import VoicePipeline
    except Exception as e:  # voice extras not installed
        bus.emit("voice_status", status="unavailable", error=f"Voice packages missing: {e}")
        return
    vp = VoicePipeline(bus.loop)
    state["voice"] = vp
    agent.speaker = vp.speak
    agent.speak_enabled = lambda: bool(vp.tts.kokoro) and config.store.load().voice_enabled
    vp.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.loop = asyncio.get_running_loop()
    load_builtin_tools()
    state["plugins"] = load_plugins(PLUGIN_DIRS)
    apps_mod.warm()
    files_mod.warm()
    from ..memory import contentindex

    contentindex.warm()
    collector = asyncio.create_task(_metrics_collector())
    from ..memory import keeper

    keeper_task = asyncio.create_task(keeper.loop(lambda: agent.busy.locked()))
    from .. import proactive

    proactive_task = asyncio.create_task(proactive.loop(lambda: agent.busy.locked()))
    from .. import devices, telegram

    telegram.start()
    asyncio.get_running_loop().run_in_executor(None, devices.auto_setup)   # printers / robot car, verified
    from .. import push

    push_task = asyncio.create_task(push.loop())
    start_voice()
    log.info("Jarvis %s ready - %d tools, plugins: %s", __version__, len(registry.tools), state["plugins"])
    yield
    collector.cancel()
    keeper_task.cancel()
    proactive_task.cancel()
    push_task.cancel()
    if state["voice"]:
        state["voice"].shutdown()


app = FastAPI(title="Jarvis", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def token_guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        # <img> tags (live camera, photos, diagrams) can't send headers, so those also accept ?token=
        tok = request.headers.get("x-jarvis-token") or request.query_params.get("token")
        if not tok or not secrets.compare_digest(tok, TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------- UI
@app.get("/")
async def index():
    html = (UI_DIR / "index.html").read_text("utf-8").replace("__JARVIS_TOKEN__", TOKEN)
    from fastapi.responses import HTMLResponse

    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/compare")
async def compare():
    return FileResponse(UI_DIR / "compare.html", headers={"Cache-Control": "no-store"})


@app.get("/compare/reference")
async def compare_reference():
    ref = Path(__file__).resolve().parents[2] / "docs" / "reference" / "orb-reference.webp"
    if not ref.exists():
        raise HTTPException(404, "reference image not found (docs/reference/orb-reference.webp)")
    return FileResponse(ref)


@app.post("/compare/snapshot")
async def compare_snapshot(request: Request):
    """Dev tool: the comparison page uploads its render so it can be diffed offline."""
    body = await request.body()
    if len(body) > 20_000_000 or body[1:4] != b"PNG":
        raise HTTPException(400, "expected a PNG")
    out = config.DATA_DIR / "compare" / "render.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return {"saved": str(out)}


app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


# ---------------------------------------------------------------------------- phone app (installable web app)
# Opened from a phone (e.g. over Tailscale: https://<pc>.<tailnet>.ts.net:8443) and added to the home screen.
@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "J.A.R.V.I.S.", "short_name": "Jarvis", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#050302", "theme_color": "#050302", "orientation": "any",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(UI_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


_icons: dict[int, bytes] = {}


@app.get("/icon-{size}.png")
@app.get("/apple-touch-icon.png")
async def icon(size: int = 180):
    """The app icon on a solid background (iOS shows transparent corners black anyway)."""
    size = size if size in (180, 192, 512) else 180
    if size not in _icons:
        import io

        from PIL import Image

        src = Image.open(UI_DIR / "jarvis-icon.png").convert("RGBA")
        bg = Image.new("RGBA", (size, size), (5, 3, 2, 255))
        inner = int(size * 0.86)
        art = src.resize((inner, inner), Image.LANCZOS)
        bg.alpha_composite(art, ((size - inner) // 2, (size - inner) // 2))
        buf = io.BytesIO()
        bg.convert("RGB").save(buf, "PNG")
        _icons[size] = buf.getvalue()
    from fastapi.responses import Response

    return Response(_icons[size], media_type="image/png", headers={"Cache-Control": "max-age=86400"})


def _decode_audio(data: bytes):
    """Any recording a browser makes (webm/opus, mp4/aac from iPhones, wav) -> 16 kHz mono float32 for Whisper."""
    import io

    import av
    import numpy as np

    out = []
    with av.open(io.BytesIO(data)) as c:
        res = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in c.decode(audio=0):
            for f in res.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in res.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    return (np.concatenate(out).astype(np.float32) / 32768.0) if out else np.zeros(0, np.float32)


DEVICE_SECRETS = ("printer_serial", "printer_code", "printer_api_key", "robot_token")


@app.get("/api/secrets")
async def secrets_status():
    return {n: bool(config.get_secret(n)) for n in DEVICE_SECRETS}


@app.post("/api/secrets")
async def secrets_set(body: dict[str, Any]):
    """Device keys only (printer serial / check code / API key, robot car token); an empty value keeps the old one."""
    for n, v in body.items():
        if n in DEVICE_SECRETS and isinstance(v, str) and v.strip():
            config.set_secret(n, v.strip())
    return {n: bool(config.get_secret(n)) for n in DEVICE_SECRETS}


@app.get("/api/push")
async def push_info():
    from .. import push

    return {"key": await asyncio.to_thread(push.public_key), "phones": push.count()}


@app.post("/api/push/subscribe")
async def push_subscribe(body: dict[str, Any]):
    from .. import push

    if not (isinstance(body.get("endpoint"), str) and body["endpoint"].startswith("https://")):
        raise HTTPException(400, "not a push subscription")
    n = push.subscribe(body)
    await asyncio.to_thread(push.send, "Jarvis", "Notifications are on ✓", "/", "welcome")
    return {"phones": n}


@app.post("/api/push/test")
async def push_test():
    from .. import push

    return {"sent": await asyncio.to_thread(push.send, "Jarvis", "Test notification - this is how I'll reach you.")}


@app.get("/api/devices/scan")
async def devices_scan(setup: bool = True):
    from .. import devices

    found = await asyncio.to_thread(devices.scan)
    if setup:
        found["set_up"] = devices.apply(found)
    return found


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request):
    """Speech from the phone's mic (whatever the browser records: webm / mp4 / wav) -> text, with Jarvis's Whisper."""
    vp = state["voice"]
    if not (vp and vp.stt.model is not None):
        raise HTTPException(503, "Voice isn't loaded on the PC (Settings → Voice).")
    data = await request.body()
    if len(data) < 200:
        return {"text": ""}

    def run() -> str:
        return vp.stt.transcribe(_decode_audio(data))
    try:
        return {"text": await asyncio.to_thread(run)}
    except Exception as e:
        raise HTTPException(400, f"Couldn't understand that recording: {e}")


@app.post("/api/tts")
async def tts_audio(body: dict[str, Any]):
    """One sentence in Jarvis's voice as a WAV, for the phone (its own speaker) - replies and spoken directions."""
    vp = state["voice"]
    text = str(body.get("text", "")).strip()[:400]
    if not (vp and vp.tts.kokoro is not None):
        raise HTTPException(503, "voice not loaded")
    if not text:
        raise HTTPException(400, "no text")

    def run() -> bytes:
        import io
        import wave

        import numpy as np

        parts, sr = [], 24000
        for chunk, sr in vp.tts._chunks(text):
            parts.append(chunk)
        pcm = (np.clip(np.concatenate(parts) if parts else np.zeros(1, np.float32), -1, 1) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()
    from fastapi.responses import Response

    return Response(await asyncio.to_thread(run), media_type="audio/wav")


# ---------------------------------------------------------------------------- settings
def _settings_payload() -> dict[str, Any]:
    s = config.store.load()
    key = config.get_api_key()
    vp = state["voice"]
    return {
        "settings": s.public(),
        "key_set": bool(key),
        "key_masked": config.mask_key(key),
        "presets": config.PROVIDER_PRESETS,
        "hardware": detect(),
        "voice_profile": voice_profile(s.voice_engine),
        "voice": {"status": vp.status if vp else "off", "error": vp.error if vp else "",
                  "voices": vp.tts.voices() if vp else []},
        "version": __version__,
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_payload()


@app.post("/api/settings")
async def post_settings(body: dict[str, Any]):
    allowed = set(config.Settings.model_fields)
    changes = {k: v for k, v in body.items() if k in allowed}
    if "provider" in changes and changes["provider"] in config.PROVIDER_PRESETS and "base_url" not in changes:
        preset = config.PROVIDER_PRESETS[changes["provider"]]["base_url"]
        if preset:
            changes["base_url"] = preset
    try:
        config.store.update(**changes)
    except Exception as e:
        raise HTTPException(400, str(e))
    apply_settings()
    return _settings_payload()


def apply_settings() -> None:
    """Make saved settings take effect now (voice, speed, voice on). Also used by the `settings` tool."""
    s = config.store.load()
    vp = state["voice"]
    if vp:
        vp.tts.voice, vp.tts.speed = s.tts_voice, s.tts_speed
        from ..voice.pipeline import output_index

        vp.tts.output_device = output_index(s.speaker_device)
        if s.tts_voice.startswith("pocket:") and vp.tts.pocket is None:
            threading.Thread(target=vp.tts._load_pocket, daemon=True, name="pocket-load").start()
    if s.voice_enabled and not vp:
        start_voice()


@app.post("/api/key")
async def post_key(body: dict[str, Any]):
    key = str(body.get("key", ""))
    try:
        config.set_api_key(key)
    except Exception as e:
        raise HTTPException(500, f"Couldn't save to the OS keychain: {e}")
    return {"key_set": bool(key.strip()), "key_masked": config.mask_key(key.strip())}


@app.post("/api/test-connection")
async def test_connection():
    try:
        return await brain.test_connection()
    except BrainError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}


@app.get("/api/models")
async def models():
    try:
        return {"models": await brain.list_models()}
    except BrainError as e:
        return {"models": [], "error": str(e), "kind": e.kind}


@app.post("/api/test-model")
async def test_model(body: dict[str, Any]):
    model = body.get("model") or config.store.load().model
    try:
        return await brain.test_tool_calling(model)
    except BrainError as e:
        return {"native": False, "works": False, "error": str(e), "kind": e.kind}


# ---------------------------------------------------------------------------- data
@app.get("/api/history")
async def history(limit: int = 50):
    return {"messages": memory.history(limit)}


@app.delete("/api/history")
async def clear_history():
    memory.clear_history()
    agent.reset()
    return {"ok": True}


@app.get("/api/undo")
async def undo_list():
    return {"entries": journal.recent(15)}


@app.post("/api/undo")
async def undo_do():
    return {"undone": journal.undo_last(1)}


@app.get("/api/tools")
async def tools():
    s = config.store.load()
    return {"tools": [
        {"name": t.name, "description": t.description, "risk": t.risk,
         "permission": permissions.effective(t.name, t.risk),
         "override": s.tool_permissions.get(t.name)}
        for t in registry.tools.values()
    ], "plugins": state["plugins"]}


@app.get("/api/skills")
async def skills_list():
    return {"skills": skills.all_skills()}


@app.get("/api/devices")
async def devices():
    try:
        from ..voice.pipeline import list_input_devices, list_output_devices

        return {"inputs": list_input_devices(), "outputs": list_output_devices()}
    except Exception:
        return {"inputs": [], "outputs": []}


# ---------------------------------------------------------------------------- CAD
_CAD_FILES = {"part.stl", "part.3mf", "part.scad", "preview.png"}


@app.get("/api/cad/file/{part}/{version}/{name}")
async def cad_file(part: str, version: int, name: str):
    from ..cad import designer

    if name not in _CAD_FILES and not (name.startswith("preview_") and name.endswith(".png")):
        raise HTTPException(404)
    if designer.slug(part) != part:
        raise HTTPException(404)
    f = designer.vdir(part, version) / name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


@app.get("/api/cad/state")
async def cad_state():
    """What the model view showed last, so a reload comes back to the same part."""
    from ..cad import designer

    s = designer.load_state()
    if not s.get("open") or not s.get("part") or not designer.versions_of(s["part"]):
        return {"open": False}
    p = dict(s)
    v = s.get("version") or designer.versions_of(s["part"])[-1]
    meta = designer.read_meta(s["part"], v)
    p.update(name=s["part"].replace("_", " "), version=v, versions=designer.versions_of(s["part"]),
             url=designer.stl_url(s["part"], v), size_mm=meta.get("size_mm"), issues=meta.get("issues", []))
    return p


@app.post("/api/cad/close")
async def cad_close():
    from ..cad import designer

    designer.save_state(open=False)
    return {"ok": True}


@app.post("/api/cad/show")
async def cad_show(body: dict[str, Any]):
    from ..cad import designer

    try:
        return designer.show(designer.resolve(str(body.get("part", ""))), body.get("version") or None)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/cad/parts")
async def cad_parts():
    from ..cad import designer

    return {"parts": designer.parts()}


# ---------------------------------------------------------------------------- cameras
def _cams():
    from ..vision import cameras

    return cameras


# ---------------------------------------------------------------------------- location / map / navigation
_last_app_fix = [0.0]
_geo_addr: dict[str, Any] = {}


@app.get("/api/geo/state")
async def geo_state():
    """Everything the map needs when it's opened by hand: where you are, today's trail, places, reminders."""
    from .. import geo

    def gather():
        me = geo.latest()
        address = None
        if me:   # a street name reads better than coordinates on the card (cached per ~50 m)
            key = (round(me["lat"], 3), round(me["lon"], 3))
            if _geo_addr.get("key") != key:
                try:
                    _geo_addr.update(key=key, address=geo.reverse(me["lat"], me["lon"]))
                except Exception:
                    _geo_addr.update(key=None, address=None)
            address = _geo_addr.get("address")
        out = {"view": "me", "me": geo.me_view(me), "address": address,
               "trail": [[p["lat"], p["lon"]] for p in geo.trail(12)],
               "places": geo.places_list(), "reminders": geo.active_reminders()}
        cur = geo.MAP_STATE
        if cur.get("view") == "route" and cur.get("route"):   # a route in progress: reopen it
            out.update(view="route", route=cur["route"])
        return out

    s = config.store.load()
    return {**(await asyncio.to_thread(gather)), "units": s.nav_units, "voice": s.nav_voice}


@app.post("/api/geo/fix")
async def geo_fix(body: dict[str, Any]):
    """This device's own GPS / Wi-Fi position (the map asks the browser for it). Kept at most every 3 s in the track
    so reminders and "where am I" stay current; the map itself uses every fix."""
    from .. import geo

    lat, lon = float(body["lat"]), float(body["lon"])
    now = time.time()
    if now - _last_app_fix[0] < 3:
        return {"ok": True, "kept": False}
    _last_app_fix[0] = now
    fired = await asyncio.to_thread(geo.record, lat, lon, body.get("acc"), body.get("heading"), True, None, "device")
    return {"ok": True, "kept": True, "reminders": fired}


@app.post("/api/geo/reroute")
async def geo_reroute(body: dict[str, Any]):
    """Off the route: a new one from where you are now to the same place, the same way (drive / walk / bike).
    check=true only looks (is there a faster way?) without changing the map."""
    from .. import geo

    cur = geo.MAP_STATE.get("route") or {}
    to, mode = body.get("to") or cur.get("to"), body.get("mode") or cur.get("mode") or "drive"
    if not to:
        raise HTTPException(400, "no route to redo")
    try:
        r = await asyncio.to_thread(geo.route, to, {"lat": float(body["lat"]), "lon": float(body["lon"])}, mode)
    except Exception as e:
        raise HTTPException(502, f"reroute failed: {e}")
    r["to"] = to
    if geo.MAP_STATE.get("view") == "route" and not body.get("check"):
        geo.MAP_STATE["route"] = r
    return {"route": r}


@app.post("/api/geo/say")
async def geo_say(body: dict[str, Any]):
    """A spoken navigation line in Jarvis's own voice. urgent=true cuts off whatever he's saying (a turn beats a
    chat reply). Returns spoken=false when voice is off, so the map can use the browser's voice instead."""
    vp = state["voice"]
    text = str(body.get("text", ""))[:300]
    if not (vp and vp.tts.kokoro is not None and text):
        return {"spoken": False}
    if body.get("urgent"):
        vp.tts.stop()
    vp.tts.say(text)
    return {"spoken": True}


@app.post("/api/geo/close")
async def geo_close():
    from .. import geo

    geo.MAP_STATE.clear()
    return {"ok": True}


@app.get("/api/telegram")
async def telegram_status():
    from .. import telegram

    return telegram.status()


@app.post("/api/telegram")
async def telegram_set(body: dict[str, Any]):
    """token: set/replace the bot token (empty removes it) · pair: get a code to send the bot · unpair."""
    from .. import telegram

    if "token" in body:
        config.set_secret(telegram.TOKEN_KEY, str(body["token"]).strip())
        if not body["token"]:
            config.store.update(telegram_chat_id=None)
        telegram.start()
        await asyncio.sleep(1.5)        # let it say hello to Telegram so the status shows the bot's name
    if body.get("unpair"):
        config.store.update(telegram_chat_id=None)
    if body.get("pair"):
        telegram.new_pair_code()
    return telegram.status()


@app.post("/api/maps-key")
async def maps_key(body: dict[str, Any]):
    from .. import geo

    config.set_secret(geo.GOOGLE_KEY, str(body.get("key", "")).strip())
    return {"set": bool(config.get_secret(geo.GOOGLE_KEY))}


@app.get("/api/maps-key")
async def maps_key_status():
    from .. import geo

    return {"set": bool(config.get_secret(geo.GOOGLE_KEY))}


@app.get("/api/camera/list")
async def camera_list():
    cams = await asyncio.to_thread(_cams().hub.all)
    s = config.store.load()
    d = s.default_camera or (cams[0].id if cams else "")
    configured = {c["id"] for c in s.cameras}
    return {"cameras": [{**c.info(), "default": c.id == d, "auto": c.id not in configured} for c in cams]}


@app.get("/api/camera/devices")
async def camera_devices():
    try:
        return {"devices": await asyncio.to_thread(_cams().detect_devices)}
    except Exception as e:
        return {"devices": [], "error": str(e)}


@app.post("/api/camera/add")
async def camera_add(body: dict[str, Any]):
    try:
        return _cams().add_camera(str(body.get("name", "")), str(body.get("kind", "")),
                                  int(body.get("device", 0) or 0), str(body.get("url", "")))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/camera/remove")
async def camera_remove(body: dict[str, Any]):
    _cams().remove_camera(str(body.get("id", "")))
    return {"ok": True}


@app.post("/api/camera/default")
async def camera_default(body: dict[str, Any]):
    config.store.update(default_camera=str(body.get("id", "")))
    return {"ok": True}


@app.get("/api/camera/{cid}/snapshot")
async def camera_snapshot(cid: str):
    from fastapi.responses import Response

    cams = _cams()
    try:
        cam = await asyncio.to_thread(cams.hub.get, cid)
        img = await asyncio.to_thread(cam.frame)
    except Exception as e:
        raise HTTPException(502, str(e))
    return Response(cams.encode_jpeg(img), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/camera/{cid}/stream")
async def camera_stream(cid: str, request: Request):
    """Live view (MJPEG) for the camera panel. Keeps the camera open while someone watches."""
    from fastapi.responses import StreamingResponse

    cams = _cams()
    try:
        cam = await asyncio.to_thread(cams.hub.get, cid)
    except Exception as e:
        raise HTTPException(404, str(e))

    async def gen():
        # every new frame as soon as it arrives (up to 25 fps); cameras read as MJPEG (the robot car) are passed
        # through untouched - no decode / re-encode, no added delay
        boundary = b"--frame\r\n"
        last = 0.0
        while not await request.is_disconnected():
            t0 = time.time()
            try:
                img = await asyncio.to_thread(cam.frame, 0.5)
                jpg, ts = cam.latest_jpeg(last)
                if jpg is None:
                    if cam._ts <= last and cam._mode != "snapshot":
                        await asyncio.sleep(0.01)
                        continue
                    ts = cam._ts or time.time()
                    jpg = await asyncio.to_thread(cams.encode_jpeg, img, 75, 960)
                last = ts
            except Exception:
                await asyncio.sleep(1)
                continue
            yield boundary + b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(max(0.0, 1 / 25 - (time.time() - t0)))

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/camera/snap/{name}")
async def camera_snap(name: str):
    from ..hands.camera import snap_path

    p = snap_path(name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- media (diagrams, pictures in chat)
@app.get("/api/file")
async def media_ref(ref: str):
    """What a MEDIA:<ref> tag in a reply points at (a snapshot, screenshot, diagram, recording, file)."""
    from .. import media

    p = media.resolve(ref)
    if p is None:
        raise HTTPException(404, "no such file")
    inline = media.kind(p) in ("image", "video")
    return FileResponse(p, filename=None if inline else p.name, headers={"Cache-Control": "no-store"})


@app.get("/api/media/{name}")
async def media_file(name: str):
    root = config.DATA_DIR / "media"
    p = (root / name).resolve()
    if p.parent != root.resolve() or not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------------------- background jobs
@app.get("/api/jobs")
async def jobs_list():
    from ..agent import jobs

    return {"jobs": jobs.all_jobs()}


@app.get("/api/jobs/{jid}")
async def jobs_get(jid: str):
    from ..agent import jobs

    try:
        return jobs.get(jid)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.post("/api/jobs/{jid}/cancel")
async def jobs_cancel(jid: str):
    from ..agent import jobs

    try:
        return jobs.cancel(jid)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/jobs/{jid}")
async def jobs_delete(jid: str):
    from ..agent import jobs

    jobs.delete(jid)
    return {"ok": True}


# ---------------------------------------------------------------------------- study decks
@app.get("/api/study")
async def study_list():
    from ..hands import study

    return {"decks": study.decks()}


@app.get("/api/study/deck/{deck}")
async def study_deck(deck: str):
    from ..hands import study

    try:
        d = study.load(deck)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"deck": d["name"], "due": [c["id"] for c in study.due(d)], "cards": d["cards"]}


@app.post("/api/study/grade")
async def study_grade(body: dict[str, Any]):
    from ..hands import study

    try:
        return study.grade(str(body.get("deck")), str(body.get("card_id")), bool(body.get("right")))
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/study/deck/{deck}")
async def study_delete(deck: str):
    from ..hands import study

    try:
        d = study.load(deck)
    except LookupError as e:
        raise HTTPException(404, str(e))
    study._path(d["name"]).unlink(missing_ok=True)
    return {"deleted": d["name"]}


# ---------------------------------------------------------------------------- proactivity
@app.get("/api/proactive")
async def proactive_status():
    from .. import proactive

    return proactive.status()


@app.post("/api/proactive/pause")
async def proactive_pause(body: dict[str, Any]):
    from .. import proactive

    return {"quiet_until": proactive.pause(float(body.get("minutes", 120)))}


# ---------------------------------------------------------------------------- memory vault
@app.get("/api/vault")
async def vault_info():
    from ..memory import keeper, vault

    return {"path": str(vault.root()), "notes": vault.summary(), "keeper": keeper.status()}


@app.post("/api/vault/open")
async def vault_open():
    from ..hands.osutil import open_path
    from ..memory import vault

    open_path(str(vault.root()))
    return {"ok": True}


@app.post("/api/vault/keep-now")
async def vault_keep_now():
    from ..memory import keeper

    try:
        return {"filed": await keeper.run_once(force=True)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/hub")
async def hub_snapshot():
    from .. import hub

    vp = state["voice"]
    return await asyncio.to_thread(hub.snapshot, vp.status if vp else "off")


@app.get("/api/stats")
async def stats():
    def summary(v: list[int]) -> dict[str, int]:
        sv = sorted(v)
        return {"n": len(sv), "p50": sv[len(sv) // 2], "max": sv[-1]} if sv else {"n": 0}

    return {k: summary(v) for k, v in metrics.items()}


# ---------------------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws(socket: WebSocket):
    if socket.query_params.get("token") != TOKEN:
        await socket.close(code=4401)
        return
    await socket.accept()
    q = bus.subscribe()
    vp = state["voice"]
    await socket.send_json({"type": "hello", "voice_status": vp.status if vp else "off",
                            "key_set": bool(config.get_api_key()), "model": config.store.load().model,
                            "local": config.store.load().provider in ("lmstudio", "ollama")})

    async def pump():
        while True:
            ev = await q.get()
            await socket.send_json(ev)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await socket.receive_json()
            t = msg.get("type")
            vp = state["voice"]
            if t == "ask":
                if vp:
                    vp.interrupt()
                src = "phone" if msg.get("source") == "phone" else "text"
                asyncio.create_task(agent.handle(str(msg.get("text", "")), source=src))
            elif t == "cancel":
                agent.cancel()
                if vp:
                    vp.tts.stop()
                bus.emit("orb", state="idle")
            elif t == "ptt":
                if vp and vp.status == "ready":
                    vp.push_to_talk()
                else:
                    bus.emit("notice", level="warn",
                             text="Voice isn't ready" + (f": {vp.error}" if vp and vp.error else "."))
            elif t == "confirm":
                permissions.confirmations.answer(str(msg.get("id")), bool(msg.get("approved")))
            elif t == "interrupt":
                if vp:
                    vp.interrupt()
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        bus.unsubscribe(q)
