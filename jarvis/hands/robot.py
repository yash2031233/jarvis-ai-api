"""The robot car: a small 4WD car running the jarvis-car firmware (an ESP32 camera board + motor driver), with its own
camera on a pan/tilt head. Found on the network by itself (its broadcast, then /ping must answer "jarvis-car").

The car keeps two reflexes on board that don't wait for Jarvis: it won't start forward towards something close and
stops by itself at ~30 cm (ultrasonic), and every move has a hard time limit (3 s), so a dropped connection can
never leave it driving. The firmware's token lives in the keychain (robot_token).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import numpy as np

from .. import config, devices
from ..events import bus
from .registry import ToolError, tool

PAN_CENTER, TILT_LEVEL = 90, 90
HEAD_PRESETS = {"center": (0, 0), "left": (-60, 0), "right": (60, 0), "far_left": (-90, 0), "far_right": (90, 0),
                "up": (0, 45), "down": (0, -10)}
CLEAR_CM = 45
DEFAULT_SPEED = 30
TURN_SPEED = 40
MOVES = {"forward": (1, 1), "back": (-1, -1), "left": (-1, 1), "right": (1, -1),
         "veer_left": (0.35, 1), "veer_right": (1, 0.35)}
PATTERNS = {"square": "forward 0.7, right 90, forward 0.7, right 90, forward 0.7, right 90, forward 0.7, right 90",
            "turn_around": "right 180", "spin": "right 360", "look_around": "right 90, right 90, right 90, right 90",
            "wiggle": "left 25, right 50, left 25"}
STEP_RE = re.compile(r"^(forward|back|left|right|veer_left|veer_right)\s+(-?[\d.]+)\s*(s|sec|seconds?|deg|degrees?|°)?$")


class CarOffline(RuntimeError):
    pass


_state_cache = {"ip": None, "ok": 0.0, "offline_until": 0.0}


def _find() -> str:
    s = config.store.load()
    if s.robot_host and devices.car_info(s.robot_host):
        return s.robot_host
    ip = devices.hear_car(4.0)
    if not (ip and devices.car_info(ip)):
        found = devices.scan(include_cameras=False)["cars"]
        ip = found[0]["host"] if found else None
    if not ip:
        raise CarOffline("The car isn't on the network - is it switched on?")
    config.store.update(robot_host=ip)
    return ip


def _call(path: str, timeout: float = 4, **params):
    import httpx

    now = time.time()
    if now < _state_cache["offline_until"]:
        raise CarOffline("The car isn't answering (checked moments ago) - is it switched on?")
    ip = _state_cache["ip"] if _state_cache["ip"] and now - _state_cache["ok"] < 8 else None
    try:
        ip = ip or _find()
    except CarOffline:
        _state_cache["offline_until"] = time.time() + 15
        raise
    headers = {"X-Token": config.get_secret("robot_token")}
    try:
        r = httpx.get(f"http://{ip}{path}", params=params, timeout=timeout, headers=headers)
    except httpx.HTTPError:
        _state_cache["ok"] = 0
        ip = _find()
        r = httpx.get(f"http://{ip}{path}", params=params, timeout=timeout, headers=headers)
    if r.status_code == 401:
        raise ToolError("The car rejected its token.", hint="Set robot_token in Settings (it's in the car's firmware).")
    _state_cache.update(ip=ip, ok=time.time())
    return r


def _state(path: str = "/status", **params) -> dict:
    r = _call(path, **params)
    d = r.json()
    d["_http"] = r.status_code
    return d


def _frame() -> np.ndarray:
    import cv2

    for _ in range(4):           # now and then a JPEG arrives cut short: throw those away
        r = _call("/capture", timeout=10)
        data = r.content.rstrip(b"\x00")
        if r.status_code == 200 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
            img = cv2.imdecode(np.frombuffer(data, np.uint8), 1)
            if img is not None:
                return img
    raise ToolError("The car's camera kept sending broken frames.")


def _show(img: np.ndarray, text: str = "") -> str:
    from ..vision import cameras
    from .camera import SNAP_DIR

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"car_{int(time.time() * 1000)}.jpg"
    (SNAP_DIR / name).write_bytes(cameras.encode_jpeg(img))
    url = f"/api/camera/snap/{name}"
    bus.emit("media", url=url, alt=text[:80] or "robot car view")
    return url


def _summary(st: dict) -> str:
    bits = []
    v = st.get("battery_v", 0) or 0
    bits.append(f"battery {v:.1f} V" if st.get("power") == "battery" else "on USB power (wheels won't turn)")
    d = st.get("distance_cm", -1)
    bits.append(f"{d:.0f} cm clear ahead" if d and d > 0 else "nothing within 3 m ahead")
    if st.get("path_blocked"):
        bits.append("PATH BLOCKED")
    return ", ".join(bits)


def _servo(pan: float, tilt: float) -> tuple[int, int]:
    s = config.store.load()
    if not s.robot_tilt_ok:
        tilt = 0
    lh = 1 if s.robot_pan_left_high else -1
    return int(max(0, min(180, round(PAN_CENTER - pan * lh)))), int(max(80, min(180, round(TILT_LEVEL + tilt))))


def _head(pan: float = 0, tilt: float = 0, settle: float = 0.35) -> tuple[int, int]:
    p, t = _servo(pan, tilt)
    _call("/look", pan=p, tilt=t)
    time.sleep(settle)
    return p, t


def _drive(move: str, seconds: float, speed: int, degrees: float | None = None) -> dict:
    if move not in MOVES:
        raise ToolError(f"move must be one of {', '.join(MOVES)}")
    if degrees and move in ("left", "right"):
        seconds, speed = abs(degrees) / float(config.store.load().robot_turn_deg_per_s or 200), TURN_SPEED
    ms = int(max(0.1, min(3.0, seconds)) * 1000)
    speed = max(20, min(100, speed))
    fl, fr = MOVES[move]
    before = _state()
    if move in ("forward", "veer_left", "veer_right"):
        d = before.get("distance_cm", -1)
        if 0 < d < CLEAR_CM:
            return {"moved": False, "why": f"blocked - something {d:.0f} cm ahead", "state": before}
    st = _state("/drive", left=int(fl * speed), right=int(fr * speed), ms=ms)
    if st["_http"] == 409:
        return {"moved": False, "why": f"blocked - the car saw something {st.get('distance_cm', 0):.0f} cm ahead", "state": st}
    t_end = time.time() + ms / 1000 + 2
    while time.time() < t_end:
        time.sleep(0.08)
        st = _state()
        if not st.get("moving"):
            break
    out = {"moved": True, "why": st.get("last_stop") or "done", "state": st}
    d0, d1 = before.get("distance_cm", -1), st.get("distance_cm", -1)
    if move in ("forward", "back") and 0 < d0 < 300 and 0 < d1 < 300:
        out["travelled_cm"] = round(abs(d0 - d1))
    return out


async def _see(img: np.ndarray, q: str, max_tokens: int = 400, max_side: int = 1024) -> str:
    from ..vision import see

    try:
        return await see.ask([img], q, max_tokens=max_tokens, max_side=max_side)
    except see.NoVision as e:
        return str(e)


LOOK_Q = ("You are looking through the camera on a small robot car, about 10 cm off the floor. Describe what is in "
          "front: objects, people, pets, obstacles, open floor, doorways. Be specific and brief.")

EXPLORE_PROMPT = """You are driving a small robot car (you see through its camera, ~10 cm off the floor).
Goal: {goal}
Sonar: {sonar}. Step {step} of {steps}.
What happened so far:
{history}

Decide the single next move. Reply with JSON only:
{{"see": "<what matters in this view, one sentence>", "done": <true if the goal is achieved or clearly impossible>,
  "move": "forward|back|left|right|veer_left|veer_right", "seconds": <0.2-2.0; turning 0.45 s is about a quarter turn>,
  "why": "<one short reason>"}}"""


@tool(risk="low", timeout=600,
      tags=["robot", "car", "drive", "robot car", "explore", "go find", "move forward", "turn around", "the car"],
      examples=["robot(action='look')", "robot(action='drive', move='forward', seconds=1)",
                "robot(action='route', plan='square')", "robot(action='explore', goal='find the cat')"])
async def robot(action: str, question: str = "", head: str = "", move: str = "forward", seconds: float = 0.8,
                degrees: float = 0, plan: str = "", speed: int = DEFAULT_SPEED, goal: str = "", steps: int = 8,
                focus: str = "", duration: int = 60, pan: float | None = None, tilt: float | None = None) -> dict:
    """The robot car on the floor, with ITS OWN camera on its head (not the desk webcam - that's `camera`).
    action: status = on? battery? anything in front? look = see through the car's camera (`question` optional;
    aim first with `head` center|left|right|far_left|far_right|up|down or `pan` -90..90 / `tilt` -10..90);
    head = just point the head; scan = sweep left to right and describe the room in one go (`steps` 3-7);
    drive = one move: `move` forward|back|left|right|veer_left|veer_right, turns take `degrees`, others `seconds`
    0.1-3; route = several moves in one call, `plan` like 'forward 0.7, right 90' or square|turn_around|spin|
    look_around|wiggle; explore = pursue a `goal` on its own ('find the cat'), up to `steps` moves;
    watch = stay parked and report what changes; stop = stop now."""
    a = action.lower().strip()
    try:
        return await _robot(a, question, head, move, seconds, degrees, plan, speed, goal, steps, focus, duration, pan, tilt)
    except CarOffline as e:
        return {"online": False, "note": f"{e} Tell the user; don't retry until they say it's on."}


async def _robot(a, question, head, move, seconds, degrees, plan, speed, goal, steps, focus, duration, pan, tilt) -> dict:
    run = asyncio.to_thread
    if a == "stop":
        return {"stopped": True, "state": _summary(await run(_state, "/stop"))}
    if a == "status":
        st = await run(_state)
        return {"online": True, "summary": _summary(st), "firmware": st.get("fw"), "ip": st.get("ip"),
                "wifi_rssi": st.get("rssi"),
                "head": "pans and tilts" if config.store.load().robot_tilt_ok else "turns left/right only (tilt servo off)"}
    if a in ("look", "head"):
        hp, ht = HEAD_PRESETS.get(head, (None, None))
        pv = pan if pan is not None else hp
        tv = tilt if tilt is not None else ht
        if pv is not None or tv is not None or a == "head":
            await run(_head, pv or 0, tv or 0)
        img = await run(_frame)
        if a == "head":
            return {"head": {"pan": pv or 0, "tilt": tv or 0}, "snapshot": _show(img)}
        seen = await _see(img, question or LOOK_Q)
        return {"seen": seen, "snapshot": _show(img, seen)}
    if a == "scan":
        import cv2

        n = max(3, min(7, steps if steps != 8 else 5))
        stops = [round(-80 + 160 * i / (n - 1)) for i in range(n)]
        frames = []
        try:
            for p in stops:
                await run(_head, p, tilt or 0, 0.45)
                f = await run(_frame)
                f = cv2.resize(f, (int(f.shape[1] * 240 / f.shape[0]), 240))
                cv2.putText(f, "C" if p == 0 else f"{'L' if p < 0 else 'R'}{abs(p)}", (8, 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
                frames += [f, np.zeros((240, 4, 3), np.uint8)]
        finally:
            try:
                await run(_head, 0, 0, 0)
            except Exception:
                pass
        strip = np.hstack(frames[:-1])
        q = (f"These are {n} views from a small robot car's camera (~10 cm off the floor), turning its head from far "
             f"left (L80) to far right (R80), each labelled with its angle. " + (f"Question: {question}. " if question else "")
             + "Say what is where using the labels (e.g. 'at L40: the door'): people, pets, objects, obstacles, "
               "open floor, doorways. Be specific and brief.")
        seen = await _see(strip, q, 420, 2400)
        return {"seen": seen, "angles": stops, "snapshot": _show(strip, seen)}
    if a == "drive":
        res = await run(_drive, move, seconds, speed, degrees or None)
        out = {"moved": res["moved"], "stopped_because": res["why"], "sensors": _summary(res["state"])}
        if "travelled_cm" in res:
            out["travelled_cm"] = res["travelled_cm"]
        try:
            out["snapshot"] = _show(await run(_frame))
        except Exception:
            pass
        return out
    if a == "route":
        plan = PATTERNS.get(plan.strip().lower(), plan)
        moves = [p.strip().lower() for p in re.split(r"[,;\n]+", plan) if p.strip()]
        if not moves or len(moves) > 24:
            raise ToolError("route: 1-24 moves like 'forward 0.7, right 90, back 0.3' or a pattern name.")
        for i, m in enumerate(moves, 1):
            if not STEP_RE.match(m):
                raise ToolError(f"route step {i} {m!r}: use '<move> <number>', e.g. 'right 90' or 'forward 0.7'.")
        done, t0 = [], time.time()
        try:
            for m in moves:
                mv, n, unit = STEP_RE.match(m).groups()
                n, unit = float(n), unit or ""
                deg = n if mv in ("left", "right") and not unit.startswith("s") else None
                res = await run(_drive, mv, 0 if deg else n, speed, deg)
                done.append(f"{m}: {'ok' if res['moved'] else 'SKIPPED'}" + (f" ({res['why']})" if res["why"] not in ("done", "requested") else ""))
        finally:
            try:
                await run(_call, "/stop", 2)
            except Exception:
                pass
        return {"moves": done, "seconds": round(time.time() - t0, 1), "sensors": _summary(await run(_state))}
    if a == "explore":
        if not goal:
            raise ToolError("explore needs a `goal`.")
        history, story = [], []
        try:
            for step in range(1, max(1, min(20, steps)) + 1):
                st = await run(_state)
                d = st.get("distance_cm", -1)
                sonar = f"{d:.0f} cm to the nearest thing ahead" if d and d > 0 else "clear for at least 3 m"
                img = await run(_frame)
                raw = await _see(img, EXPLORE_PROMPT.format(goal=goal, sonar=sonar, step=step, steps=steps,
                                                            history="\n".join(history[-6:]) or "(nothing yet)"), 260)
                m = re.search(r"\{.*\}", raw, re.S)
                try:
                    p = json.loads(m.group(0)) if m else {}
                except json.JSONDecodeError:
                    p = {}
                snap = _show(img, p.get("see", ""))
                if not p:
                    story.append({"step": step, "note": f"couldn't read a plan: {raw[:120]}", "snapshot": snap})
                    break
                entry: dict[str, Any] = {"step": step, "saw": p.get("see", ""), "snapshot": snap}
                if p.get("done"):
                    entry["result"] = p.get("why") or "goal reached"
                    story.append(entry)
                    break
                res = await run(_drive, p.get("move", "left"), float(p.get("seconds", 0.4)), DEFAULT_SPEED)
                entry.update(move=p.get("move"), seconds=p.get("seconds"), why=p.get("why"), outcome=res["why"])
                story.append(entry)
                history.append(f"{step}. saw: {entry['saw']} -> {entry['move']} {entry['seconds']}s ({res['why']})")
        finally:
            try:
                await run(_call, "/stop", 2)
            except Exception:
                pass
        return {"goal": goal, "steps_taken": len(story), "finished": bool(story and story[-1].get("result")), "story": story}
    if a == "watch":
        t0 = last_new = time.time()
        prev, events = None, []
        limit = max(5, min(300, duration))
        while time.time() - t0 < limit and time.time() - last_new < 15:
            img = await run(_frame)
            small = np.asarray(img[::16, ::16].mean(axis=2), np.float32)
            if prev is not None and float(np.mean(np.abs(small - prev))) < 6:
                await asyncio.sleep(0.5)
                continue
            prev = small
            seen = await _see(img, "You are a robot car's camera watching a room from the floor. In one sentence, what "
                                   "is happening right now?" + (f" Pay attention to: {focus}." if focus else ""), 90)
            if not events or events[-1]["seen"] != seen:
                events.append({"t": round(time.time() - t0, 1), "seen": seen, "snapshot": _show(img, seen)})
                last_new = time.time()
        return {"events": events, "seconds": round(time.time() - t0, 1)}
    raise ToolError("action must be status, look, head, scan, drive, route, explore, watch or stop.")
