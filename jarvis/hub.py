"""The dashboard (Hub): everything at a glance - this computer, the network, Jarvis, jobs and timers, the 3D printer,
the robot car, where you are and the weather. Cheap things are read every call; slow ones (printer, car, weather,
GPU) are cached for a few seconds so the screen can refresh every 3 s without hammering anything."""

from __future__ import annotations

import socket
import subprocess
import time
from typing import Any

import psutil

from . import config

STARTED = time.time()
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    t, v = _cache.get(key, (0.0, None))
    if time.time() - t < ttl:
        return v
    try:
        v = fn()
    except Exception as e:
        v = {"error": str(e)[:160]}
    _cache[key] = (time.time(), v)
    return v


def _gpu() -> list[dict]:
    out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    gpus = []
    for line in out.stdout.strip().splitlines():
        name, util, temp, used, total = [x.strip() for x in line.split(",")]
        gpus.append({"name": name, "util": int(util), "temp_c": int(temp), "vram_used_gb": round(int(used) / 1024, 1),
                     "vram_gb": round(int(total) / 1024, 1)})
    return gpus


def _net() -> dict:
    ips, tailscale = [], None
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                if a.address.startswith("100."):
                    tailscale = a.address
                else:
                    ips.append(a.address)
    t0 = time.time()
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        ping = round((time.time() - t0) * 1000)
    except OSError:
        ping = None
    return {"local_ip": ips[0] if ips else None, "tailscale": tailscale, "internet_ms": ping}


def system() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(config.DATA_DIR.anchor or "/"))
    out: dict[str, Any] = {"cpu": psutil.cpu_percent(interval=None), "ram_used_gb": round(vm.used / 2**30, 1),
                           "ram_gb": round(vm.total / 2**30, 1), "disk_free_gb": round(du.free / 2**30),
                           "disk_gb": round(du.total / 2**30), "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1)}
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if b:
        out["battery"] = {"pct": round(b.percent), "plugged": b.power_plugged}
    out["gpus"] = _cached("gpu", 2.5, _gpu)
    return out


def _printer() -> dict | None:
    if not config.store.load().printer_kind:
        return None
    from . import devices
    from .cad import printer

    s = config.store.load()

    def get():
        # only the known address: the dashboard never sweeps the network (that's for Find devices / printing)
        port = {"moonraker": 7125, "octoprint": 80}.get(s.printer_kind, printer.FF_API)
        if not (s.printer_host and devices._open(s.printer_host.split(":")[0], port, 0.8)):
            return {"online": False, "kind": s.printer_kind}
        return {"online": True, **printer.status()}
    return _cached("printer", 10, get)


def _car() -> dict | None:
    s = config.store.load()
    if not s.robot_host:
        return None

    def get():
        import httpx

        d = httpx.get(f"http://{s.robot_host}/status", timeout=1.5,
                      headers={"X-Token": config.get_secret("robot_token")}).json()
        return {"online": True, "battery_v": d.get("battery_v"), "power": d.get("power"),
                "distance_cm": d.get("distance_cm")}
    v = _cached("car", 10, get)
    return {"online": False} if isinstance(v, dict) and "error" in v else v


def _weather() -> dict | None:
    import asyncio

    from .hands.weather import get_weather

    def get():
        w = asyncio.run(get_weather(city=config.store.load().home_city, days=1))
        f = w["forecast"][0]
        return {"place": w["place"], "now": w["now"]["temp"], "conditions": w["now"]["conditions"], "high": f["high"],
                "low": f["low"], "rain_chance": f["rain_chance"], "units": w["units"]}
    return _cached("weather", 600, get)


def _location() -> dict | None:
    from . import geo

    p = geo.latest()
    if not p:
        return None
    return {"lat": p["lat"], "lon": p["lon"], "age": geo.ago(time.time() - p["t"]), "source": p.get("src")}


def snapshot(voice_status: str = "") -> dict:
    from .agent import jobs
    from .hands.registry import registry
    from .hands.timers import list_timers_sync

    s = config.store.load()
    running = [j for j in jobs.all_jobs() if j.get("status") in ("queued", "running")]
    return {
        "time": time.time(),
        "system": system(),
        "network": _cached("net", 15, _net),
        "jarvis": {"provider": s.provider, "model": s.model, "voice": voice_status, "tools": len(registry.tools),
                   "uptime_min": round((time.time() - STARTED) / 60)},
        "jobs": [{"task": j["task"][:80], "status": j["status"]} for j in running][:5],
        "timers": [{"label": t.get("label") or "timer", "remaining": t.get("remaining")} for t in list_timers_sync()][:6],
        "printer": _printer(),
        "car": _car(),
        "location": _location(),
        "weather": _weather(),
    }
