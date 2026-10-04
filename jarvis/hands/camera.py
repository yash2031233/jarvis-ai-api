"""Camera tool: Jarvis's eyes - a directly connected webcam or an IP camera.

look     glance now and describe it / answer `question`
watch    follow what happens: glances only when the picture changes, keeps what's new, summarizes at the end
read     pages / labels held up to the camera: waits for the picture to hold still, then OCR (vision as fallback)
presence is anyone there (fast face check, vision as fallback)
snapshot save a photo
list     the cameras that are set up
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np

from .. import config
from ..events import bus
from ..vision import cameras, ocr, see
from .registry import ToolError, tool

SNAP_DIR = config.DATA_DIR / "camera"
LOOK_SYSTEM = ("You are Jarvis looking through a camera. Describe what you see plainly and briefly, or answer the "
               "question asked. Don't invent details you can't see.")


def _save(img: np.ndarray, cam: cameras.Camera, note: str = "") -> dict:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{cam.id}_{time.strftime('%Y%m%d-%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.jpg"
    (SNAP_DIR / name).write_bytes(cameras.encode_jpeg(img))
    _prune()
    bus.emit("camera_frame", cam=cam.id, name=cam.name, img=f"/api/camera/snap/{name}", text=note, t=time.time())
    return {"path": str(SNAP_DIR / name), "url": f"/api/camera/snap/{name}"}


def _prune(keep: int = 200) -> None:
    files = sorted(SNAP_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


def _open_panel(cam: cameras.Camera, action: str) -> None:
    bus.emit("camera", cam=cam.id, name=cam.name, action=action)


def _small_gray(img: np.ndarray) -> np.ndarray:
    import cv2

    g = cv2.cvtColor(cv2.resize(img, (96, 54), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (3, 3), 0).astype(np.int16)


def _changed_box(a: np.ndarray, b: np.ndarray, full: tuple[int, int]) -> tuple[float, tuple[int, int, int, int]]:
    """How much changed (0..1 share of pixels) and where (box in full-size pixels)."""
    diff = np.abs(a - b) > 18
    share = float(diff.mean())
    if not diff.any():
        return 0.0, (0, 0, full[1], full[0])
    ys, xs = np.nonzero(diff)
    H, W = full
    sx, sy = W / diff.shape[1], H / diff.shape[0]
    x0, x1 = int(xs.min() * sx), int((xs.max() + 1) * sx)
    y0, y1 = int(ys.min() * sy), int((ys.max() + 1) * sy)
    # pad so the model sees context around the change
    pw, ph = max(80, (x1 - x0) // 3), max(80, (y1 - y0) // 3)
    return share, (max(0, x0 - pw), max(0, y0 - ph), min(W, x1 + pw), min(H, y1 + ph))


async def _frame(cam: cameras.Camera) -> np.ndarray:
    return await asyncio.to_thread(cam.frame)


@tool(
    risk="low", readonly=True, timeout=420,
    tags=["camera", "webcam", "look", "see", "watch", "desk", "show", "holding", "room", "read", "presence", "ip camera"],
    examples=["camera(action='look', question='what am I holding?')", "camera(action='watch', focus='my hands')",
              "camera(action='look', camera='front door')"],
)
async def camera(action: str = "look", camera: str = "", question: str = "", focus: str = "",
                 seconds: int = 60, idle_stop: int = 10) -> dict:
    """Jarvis's eyes: the webcam or an IP camera (pick with `camera` = its name; empty = the default).
    action: look = glance now and describe it, or answer `question` ('what's on my desk', 'is the door open');
    watch = follow what's happening live - glances whenever the picture changes, keeps only what's new, stops
    after `idle_stop` s with nothing new (or `seconds`) and summarizes ('watch me', 'let me show you', `focus`
    = what to pay attention to); read = text on a page/label held up (waits for it to be still);
    presence = is anyone there; snapshot = save a photo; list = the cameras set up."""
    a = action.lower().strip()
    try:
        if a == "list":
            cams = await asyncio.to_thread(cameras.hub.all)
            d = config.store.load().default_camera
            return {"cameras": [{**c.info(), "default": c.id == d or (not d and i == 0)} for i, c in enumerate(cams)]}
        cam = await asyncio.to_thread(cameras.hub.get, camera)
        if a in ("look", "see", "describe"):
            _open_panel(cam, "look")
            img = await _frame(cam)
            q = question.strip() or "What do you see?"
            text = await see.ask([img], q, LOOK_SYSTEM)
            snap = _save(img, cam, text)
            return {"camera": cam.name, "answer": text, "photo": snap["path"]}
        if a in ("snapshot", "photo", "picture"):
            _open_panel(cam, "snapshot")
            img = await _frame(cam)
            snap = _save(img, cam, "snapshot")
            return {"camera": cam.name, "photo": snap["path"], "size": [int(img.shape[1]), int(img.shape[0])]}
        if a == "presence":
            img = await _frame(cam)
            n = await asyncio.to_thread(_faces, img)
            if n:
                return {"camera": cam.name, "someone_there": True, "faces": n, "method": "face detection"}
            text = await see.ask([img], "Is there a person visible? Answer 'yes' or 'no' and, if yes, where.",
                                 max_tokens=60, max_side=640)
            return {"camera": cam.name, "someone_there": text.lower().startswith("yes"), "detail": text,
                    "method": "vision"}
        if a == "read":
            _open_panel(cam, "read")
            img = await _steady(cam)
            try:
                lines = await asyncio.to_thread(ocr.read_image, img)
                text = ocr.text_of(lines)
            except ocr.OCRUnavailable:
                text = ""
            if len(text.strip()) < 8:  # OCR found little: let the model read it
                text = await see.ask([img], "Read all the text you can see, exactly, in reading order.",
                                     max_tokens=1200)
                method = "vision"
            else:
                method = "ocr"
            snap = _save(img, cam, text[:300])
            return {"camera": cam.name, "text": text, "method": method, "photo": snap["path"]}
        if a == "watch":
            return await _watch(cam, focus or question, max(5, min(seconds, 300)), max(4, min(idle_stop, 60)))
        raise ToolError(f"Unknown action '{action}'.", hint="look, watch, read, presence, snapshot, list")
    except cameras.CameraError as e:
        raise ToolError(str(e), hint="Check Settings → Cameras (test the camera there).")
    except see.NoVision as e:
        raise ToolError(str(e), hint="Use action=read (OCR) or snapshot, which don't need a vision model.")


def _faces(img: np.ndarray) -> int:
    """Cheap local people check: Haar faces (OpenCV 4) or the HOG person detector; 0 = not sure -> ask the model."""
    import cv2

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if not casc.empty():
            return len(casc.detectMultiScale(g, 1.15, 5, minSize=(48, 48)))
    if hasattr(cv2, "HOGDescriptor"):
        try:
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            small = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
            rects, weights = hog.detectMultiScale(small, winStride=(8, 8))
            return int(sum(1 for w in np.ravel(weights) if w > 0.6))
        except Exception:
            return 0
    return 0


async def _steady(cam: cameras.Camera, timeout: float = 8.0) -> np.ndarray:
    """Wait until the picture holds still (a page being held up), then return it."""
    prev = None
    t0 = time.time()
    img = await _frame(cam)
    while time.time() - t0 < timeout:
        img = await _frame(cam)
        g = _small_gray(img)
        if prev is not None and float((np.abs(g - prev) > 14).mean()) < 0.01:
            return img
        prev = g
        await asyncio.sleep(0.25)
    return img


async def _watch(cam: cameras.Camera, focus: str, seconds: int, idle_stop: int) -> dict:
    from rapidfuzz import fuzz

    _open_panel(cam, "watch")
    t0 = time.time()
    events: list[dict] = []
    last_new = time.time()
    last_glance = 0.0
    base = None
    img = await _frame(cam)
    H, W = img.shape[:2]
    prompt = ("In ONE short sentence, what is happening or what changed?" +
              (f" Pay attention to: {focus}." if focus else "") +
              " If nothing meaningful is happening, reply exactly: nothing")
    # first look at the whole scene
    first = await see.ask([img], "In one sentence, what do you see?" + (f" Focus on: {focus}." if focus else ""),
                          max_tokens=80, max_side=768)
    events.append({"t": 0.0, "what": first})
    _save(img, cam, first)
    base = _small_gray(img)
    pending: set[asyncio.Task] = set()

    async def glance(frame: np.ndarray, box: tuple[int, int, int, int], at: float) -> None:
        nonlocal last_new
        x0, y0, x1, y1 = box
        crop = frame[y0:y1, x0:x1] if (x1 - x0) * (y1 - y0) < 0.7 * W * H else frame
        text = await see.ask([crop], prompt, max_tokens=60, max_side=640)
        t = text.strip().strip(".")
        if not t or t.lower().startswith("nothing"):
            return
        if events and fuzz.token_set_ratio(t.lower(), events[-1]["what"].lower()) >= 80:
            return  # same as last time
        events.append({"t": round(at - t0, 1), "what": t})
        last_new = time.time()
        _save(frame, cam, t)

    while time.time() - t0 < seconds:
        idle_limit = idle_stop if len(events) > 1 else max(idle_stop, 20)
        if time.time() - last_new > idle_limit and not pending:
            break
        img = await _frame(cam)
        g = _small_gray(img)
        share, box = _changed_box(g, base, (H, W))
        if share > 0.02 and time.time() - last_glance > 1.2 and len(pending) < 2:
            last_glance = time.time()
            base = g
            task = asyncio.create_task(glance(img, box, time.time()))
            pending.add(task)
            task.add_done_callback(pending.discard)
        await asyncio.sleep(0.2)
    if pending:
        await asyncio.wait(pending, timeout=15)
    log_text = "\n".join(f"[{e['t']}s] {e['what']}" for e in events)
    from ..brain.client import brain

    summary, _ = await brain.complete([
        {"role": "system", "content": "Summarize what was seen through a camera, in 1-3 natural sentences."},
        {"role": "user", "content": (f"Focus: {focus}\n" if focus else "") + log_text},
    ], max_tokens=200)
    bus.emit("camera", cam=cam.id, name=cam.name, action="watch_done")
    return {"camera": cam.name, "watched_seconds": round(time.time() - t0), "events": events, "summary": summary}


def snap_path(name: str) -> Path | None:
    p = SNAP_DIR / name
    return p if p.parent == SNAP_DIR and p.exists() else None
