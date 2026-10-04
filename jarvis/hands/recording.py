"""Screen recording: capture the next N seconds of the screen (or one window) to an .mp4, and optionally have the
vision model look at it - frames sampled across the recording, so it can describe what changed over time.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np

from .. import config
from .registry import ToolError, tool

REC_DIR = config.DATA_DIR / "recordings"


def _record(seconds: float, window: str, fps: int) -> tuple[str, list[tuple[float, np.ndarray]]]:
    import cv2

    from .screen import grab

    REC_DIR.mkdir(parents=True, exist_ok=True)
    out = REC_DIR / f"screen_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    first, _, _ = grab(window)
    h, w = first.shape[:2]
    scale = min(1.0, 1920 / max(w, h))                 # keep files reasonable on 4K / multi-monitor setups
    size = (int(w * scale) // 2 * 2, int(h * scale) // 2 * 2)
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    frames: list[tuple[float, np.ndarray]] = []
    t0 = time.time()
    n = 0
    try:
        while time.time() - t0 < seconds:
            img, _, _ = grab(window)
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h))
            vw.write(cv2.resize(img, size))
            if n % max(1, fps) == 0:                     # keep ~1 frame/second for analysis
                frames.append((round(time.time() - t0, 1), img))
            n += 1
            time.sleep(max(0.0, t0 + n / fps - time.time()))
    finally:
        vw.release()
    _prune()
    return str(out), frames


def _prune(keep: int = 20) -> None:
    files = sorted(REC_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


def _sheet(frames: list[tuple[float, np.ndarray]], k: int = 6) -> np.ndarray:
    """k evenly spaced frames in a 3-column grid, each stamped with its time."""
    import cv2

    pick = [frames[int(i * (len(frames) - 1) / max(1, k - 1))] for i in range(min(k, len(frames)))]
    tiles = []
    for t, img in pick:
        tile = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
        cv2.rectangle(tile, (0, 0), (90, 30), (0, 0, 0), -1)
        cv2.putText(tile, f"{t:.0f}s", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        tiles.append(tile)
    th = max(t.shape[0] for t in tiles)
    tiles = [np.pad(t, ((0, th - t.shape[0]), (0, 0), (0, 0))) for t in tiles]
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)]
    return np.vstack(rows)


@tool(risk="low", timeout=200, tags=["record", "recording", "video", "screen", "capture", "watch my screen"],
      examples=["screen_record(seconds=15)", "screen_record(seconds=20, question='what goes wrong when I click Submit?')"])
async def screen_record(seconds: int = 10, window: str = "", fps: int = 10, question: str = "") -> dict:
    """Record a VIDEO of the screen (or one window by title) for the next `seconds` (max 120) and save it as .mp4.
    Only when the user asks for a recording, or to see something that changes over time. With `question`, the
    vision model also looks at frames across the recording and answers it."""
    seconds = max(2, min(int(seconds), 120))
    fps = max(2, min(int(fps), 30))
    try:
        path, frames = await asyncio.to_thread(_record, seconds, window, fps)
    except ImportError:
        raise ToolError("Recording needs OpenCV and mss: pip install opencv-python mss")
    out: dict = {"video": path, "seconds": seconds}
    if question and frames:
        from ..vision import see

        try:
            out["answer"] = await see.ask(
                [_sheet(frames)],
                f"These are frames from a {seconds}s screen recording, in time order (time in the corner). {question}",
                max_tokens=600, max_side=1920)
        except see.NoVision as e:
            out["answer"] = str(e)
    return out
