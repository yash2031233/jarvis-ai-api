"""Cameras: a directly connected webcam (USB / built-in, by device index) or an IP camera by address.

IP cameras: rtsp://..., http(s) MJPEG streams, or http(s) snapshot URLs that return one JPEG per request
(auto-detected from the response). Credentials in the URL are kept in the OS keychain.

A camera is opened on first use and released again after ~20 s without use, so other apps can have the
webcam back. Streams are read on a background thread that keeps only the newest frame (no buffering lag).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import config

log = logging.getLogger(__name__)
IDLE_RELEASE = 20.0
AUTO_WEBCAM = {"id": "webcam", "name": "Webcam", "kind": "device", "device": 0}


class CameraError(RuntimeError):
    pass


def _cv2():
    try:
        import cv2

        return cv2
    except ImportError as e:
        raise CameraError("Cameras need OpenCV: pip install opencv-python") from e


@dataclass
class Camera:
    id: str
    name: str
    kind: str  # "device" | "ip" | "car" (the robot car's own camera)
    device: int = 0
    url: str = ""
    # runtime
    _cap: Any = None
    _thread: threading.Thread | None = None
    _frame: np.ndarray | None = None
    _jpeg: bytes | None = None
    _ts: float = 0.0
    _last_use: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _mode: str = ""  # "stream" | "snapshot"
    _error: str = ""

    # ------------------------------------------------------------------ opening
    def _open(self) -> None:
        cv2 = _cv2()
        if self.kind == "device":
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if hasattr(cv2, "CAP_DSHOW") else [cv2.CAP_ANY]
            import sys

            if sys.platform != "win32":
                backends = [cv2.CAP_ANY]
            for be in backends:
                cap = cv2.VideoCapture(self.device, be)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                    self._cap, self._mode = cap, "stream"
                    return
                cap.release()
            raise CameraError(f"Couldn't open camera device {self.device} (in use by another app, or not connected).")
        url = self.url
        if not url:
            raise CameraError(f"Camera '{self.name}' has no address.")
        if self.kind == "car":
            # the car streams MJPEG on :81/stream (~20 fps); one photo per request (/capture) is the fallback
            self._mode = "mjpeg"
            return
        if url.lower().startswith("http") and self._is_snapshot(url):
            self._mode = "snapshot"
            return
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"Couldn't connect to {config.mask_url(url)} - check the address, that the camera is "
                              "on the same network, and the username/password.")
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._cap, self._mode = cap, "stream"

    @staticmethod
    def _is_snapshot(url: str) -> bool:
        if re.search(r"\.(jpe?g|png)(\?|$)", url, re.I) or re.search(r"snap|shot|still|image|picture", url, re.I):
            return True
        import httpx

        try:
            with httpx.stream("GET", url, timeout=5, follow_redirects=True) as r:
                ctype = r.headers.get("content-type", "").lower()
                return ctype.startswith("image/")
        except Exception:
            return False

    def _mjpeg_reader(self) -> None:
        """Read an MJPEG stream by its JPEG markers - lighter and lower-latency than going through ffmpeg - and keep
        the newest frame both decoded and as the original JPEG (the live view passes that straight through)."""
        import httpx

        cv2 = _cv2()
        host = self.url.split("//", 1)[1].split("/", 1)[0].split(":")[0]
        stream_url = f"http://{host}:81/stream"
        params = {"token": config.get_secret("robot_token")} if self.kind == "car" else None
        try:
            with httpx.stream("GET", stream_url, params=params, timeout=httpx.Timeout(5, read=10)) as r:
                if r.status_code != 200:
                    raise CameraError(f"stream answered {r.status_code}")
                buf = b""
                for chunk in r.iter_raw():                 # as it arrives - a fixed chunk size waits for it to fill
                    if time.time() - self._last_use > IDLE_RELEASE or self._mode != "mjpeg":
                        break
                    buf += chunk
                    end = buf.rfind(b"\xff\xd9")
                    if end < 0:
                        if len(buf) > 2_000_000:
                            buf = b""
                        continue
                    start = buf.rfind(b"\xff\xd8", 0, end)
                    if start >= 0:
                        jpg = buf[start:end + 2]
                        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            with self._lock:
                                self._frame, self._jpeg, self._ts = img, jpg, time.time()
                    buf = buf[end + 2:]
        except Exception as e:
            if self._frame is None:
                self._mode = "snapshot"          # no stream from this car: fall back to one photo per request
            else:
                self._error = f"The car's video stopped: {e}"
        finally:
            self._thread = None
            if self._mode == "mjpeg":
                self._mode = ""                   # reopen on the next request

    def latest_jpeg(self, after: float = 0.0) -> tuple[bytes | None, float]:
        """The newest original JPEG (streams read by marker only) if it's newer than `after`."""
        with self._lock:
            if self._jpeg is not None and self._ts > after:
                return self._jpeg, self._ts
        return None, self._ts

    def _reader(self) -> None:
        fails = 0
        while True:
            if time.time() - self._last_use > IDLE_RELEASE:
                break
            cap = self._cap
            if cap is None:
                break
            ok, frame = cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._frame, self._ts = frame, time.time()
                fails = 0
            else:
                fails += 1
                if fails > 50:
                    self._error = "The camera stopped sending frames."
                    break
                time.sleep(0.05)
        self.release()

    def release(self) -> None:
        if self._mode == "mjpeg":
            self._mode = ""                       # the stream reader stops at its next chunk
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self._thread = None

    # ------------------------------------------------------------------ frames
    def frame(self, max_age: float = 1.0, timeout: float = 8.0) -> np.ndarray:
        """The newest frame (BGR). Opens the camera if needed."""
        self._last_use = time.time()
        if self._mode == "snapshot":
            return self._snapshot()
        if self._mode == "mjpeg" and self._thread is None:
            self._mode = ""
        if self._cap is None and self._thread is None:
            self._error = ""
            self._open()
            if self._mode == "snapshot":
                return self._snapshot()
            target = self._mjpeg_reader if self._mode == "mjpeg" else self._reader
            self._thread = threading.Thread(target=target, daemon=True, name=f"cam-{self.id}")
            self._thread.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._frame is not None and time.time() - self._ts <= max_age:
                    return self._frame.copy()
            if self._error:
                raise CameraError(self._error)
            if self._mode == "snapshot":         # the stream wasn't there after all
                return self._snapshot()
            time.sleep(0.03)
        raise CameraError(f"No picture from '{self.name}' within {timeout:.0f}s.")

    def _snapshot(self) -> np.ndarray:
        import httpx

        cv2 = _cv2()
        headers = {"X-Token": config.get_secret("robot_token")} if self.kind == "car" else None
        img = None
        for _ in range(3 if self.kind == "car" else 1):
            try:
                r = httpx.get(self.url, timeout=8, follow_redirects=True, headers=headers)
                r.raise_for_status()
            except Exception as e:
                if self.kind == "car":
                    raise CameraError("The robot car isn't answering - is it switched on?") from e
                raise CameraError(f"Couldn't get a picture from {config.mask_url(self.url)}: {e}") from e
            data = r.content.rstrip(b"\x00")
            if self.kind == "car" and data[-2:] != b"\xff\xd9":
                continue                       # the car's camera now and then sends a JPEG cut short: take another
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                break
        if img is None:
            raise CameraError("The camera's reply wasn't an image.")
        with self._lock:
            self._frame, self._ts = img, time.time()
        return img

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "device": self.device if self.kind == "device" else None,
                "address": config.mask_url(self.url) if self.kind == "ip" else
                self.url.rsplit("/", 1)[0].replace("http://", "") if self.kind == "car" else None,
                "open": self._cap is not None}


class Hub:
    def __init__(self) -> None:
        self.cams: dict[str, Camera] = {}
        self.lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        s = config.store.load()
        cams = list(s.cameras)
        # the built-in/first webcam is always there unless a direct camera was set up or it was removed
        if s.auto_webcam and not any(c.get("kind") == "device" for c in cams):
            cams.insert(0, dict(AUTO_WEBCAM))
        if s.robot_host:                       # the robot car's camera, once the car has been found
            cams.append({"id": "car", "name": "Robot car", "kind": "car"})
        return cams

    def all(self) -> list[Camera]:
        with self.lock:
            wanted = self._load()
            ids = {c["id"] for c in wanted}
            for cid in list(self.cams):
                if cid not in ids:
                    self.cams.pop(cid).release()
            for c in wanted:
                cam = self.cams.get(c["id"])
                url = (config.get_secret(f"camera:{c['id']}") if c.get("kind") == "ip" else
                       f"http://{config.store.load().robot_host}/capture" if c.get("kind") == "car" else "")
                if cam is None or cam.kind != c["kind"] or cam.device != c.get("device", 0) or cam.url != url:
                    if cam:
                        cam.release()
                    cam = Camera(id=c["id"], name=c.get("name") or c["id"], kind=c.get("kind", "device"),
                                 device=int(c.get("device", 0) or 0), url=url)
                    self.cams[c["id"]] = cam
                else:
                    cam.name = c.get("name") or cam.name
            return [self.cams[c["id"]] for c in wanted]

    def get(self, which: str = "") -> Camera:
        cams = self.all()
        if not cams:
            raise CameraError("No cameras set up. Add one in Settings → Cameras.")
        if not which:
            d = config.store.load().default_camera
            return next((c for c in cams if c.id == d), cams[0])
        w = which.lower().strip()
        for c in cams:
            if w in (c.id.lower(), c.name.lower()):
                return c
        from rapidfuzz import fuzz, process

        hit = process.extractOne(w, {c.id: c.name.lower() for c in cams}, scorer=fuzz.WRatio)
        if hit and hit[1] >= 70:
            return next(c for c in cams if c.id == hit[2])
        raise CameraError(f"No camera called '{which}'. Cameras: {', '.join(c.name for c in cams)}")


hub = Hub()


# ------------------------------------------------------------------ settings helpers (used by the server)

def add_camera(name: str, kind: str, device: int = 0, url: str = "") -> dict[str, Any]:
    if kind not in ("device", "ip"):
        raise CameraError("kind must be 'device' or 'ip'")
    if kind == "ip" and not re.match(r"^(rtsp|rtsps|rtmp|http|https)://", url.strip(), re.I):
        raise CameraError("An IP camera address starts with rtsp://, http:// or https://")
    cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "camera"
    s = config.store.load()
    existing = {c["id"] for c in s.cameras}
    if cid in existing:
        cid = f"{cid}-{uuid.uuid4().hex[:4]}"
    entry: dict[str, Any] = {"id": cid, "name": name.strip() or cid, "kind": kind}
    if kind == "device":
        entry["device"] = int(device)
    else:
        config.set_secret(f"camera:{cid}", url.strip())
        entry["url_display"] = config.mask_url(url.strip())
    current = hub.all()
    # keep the existing default (incl. the auto webcam); only the very first camera becomes the default
    config.store.update(cameras=[*s.cameras, entry], default_camera=s.default_camera or ("" if current else cid))
    return entry


def remove_camera(cid: str) -> None:
    s = config.store.load()
    if cid == AUTO_WEBCAM["id"] and not any(c["id"] == cid for c in s.cameras):
        config.store.update(auto_webcam=False, default_camera="" if s.default_camera == cid else s.default_camera)
        return
    config.set_secret(f"camera:{cid}", "")
    cams = [c for c in s.cameras if c["id"] != cid]
    config.store.update(cameras=cams, default_camera="" if s.default_camera == cid else s.default_camera)


def detect_devices(max_index: int = 5) -> list[dict[str, Any]]:
    """Directly connected cameras that open and give a picture."""
    cv2 = _cv2()
    import sys

    found = []
    busy = {c.device for c in hub.cams.values() if c.kind == "device" and c._cap is not None}
    for i in range(max_index):
        if i in busy:
            found.append({"device": i, "name": f"Camera {i}", "in_use_by_jarvis": True})
            continue
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(i)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            found.append({"device": i, "name": f"Camera {i}"})
    return found


def encode_jpeg(img: np.ndarray, quality: int = 82, max_side: int = 1280) -> bytes:
    cv2 = _cv2()
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise CameraError("couldn't encode the picture")
    return buf.tobytes()
