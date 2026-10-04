"""Local server: serves the orb UI and bridges it to the agent + voice over a WebSocket.

Binds to 127.0.0.1 only. A per-launch token protects the API from other local web pages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
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
    collector = asyncio.create_task(_metrics_collector())
    start_voice()
    log.info("Jarvis %s ready - %d tools, plugins: %s", __version__, len(registry.tools), state["plugins"])
    yield
    collector.cancel()
    if state["voice"]:
        state["voice"].shutdown()


app = FastAPI(title="Jarvis", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def token_guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.headers.get("x-jarvis-token") != TOKEN:
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
        s = config.store.update(**changes)
    except Exception as e:
        raise HTTPException(400, str(e))
    vp = state["voice"]
    if vp:
        vp.tts.voice, vp.tts.speed = s.tts_voice, s.tts_speed
    if s.voice_enabled and not vp:
        start_voice()
    return _settings_payload()


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
        from ..voice.pipeline import list_input_devices

        return {"inputs": list_input_devices()}
    except Exception:
        return {"inputs": []}


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
                            "key_set": bool(config.get_api_key()), "model": config.store.load().model})

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
                asyncio.create_task(agent.handle(str(msg.get("text", "")), source="text"))
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
