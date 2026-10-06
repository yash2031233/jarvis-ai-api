"""Ask a model about images. The main model is used when it can see; otherwise (e.g. GLM, DeepSeek) a vision model
from the same provider takes over just for the looking - car driving, cameras, the screen, checking 3D parts -
while the main model keeps doing everything else. Settings → Brain → Vision model picks one by hand."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import numpy as np

from .. import config
from ..brain.client import BrainError, brain

log = logging.getLogger(__name__)

# Vision models to fall back to, best first (fast + reliably right on robot-car driving scenes, tested 2026-10).
PREFERRED = {
    "nvidia": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "moonshotai/kimi-k3", "moonshotai/kimi-k2.6",
               "meta/llama-3.2-90b-vision-instruct", "meta/llama-3.2-11b-vision-instruct"],
}
VISION_WORDS = ("vl", "vision", "omni", "llava", "vila", "gemma-3", "pixtral", "minicpm-v", "kimi-k3", "kimi-k2.6")
_blind: set[str] = set()                  # models that turned images down this session
_models: tuple[float, list[str]] = (0.0, [])


class NoVision(RuntimeError):
    pass


def jpeg_b64(img: np.ndarray, max_side: int = 1024, quality: int = 85) -> str:
    import cv2

    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("couldn't encode image")
    return base64.b64encode(buf.tobytes()).decode()


async def _available() -> list[str]:
    global _models
    if time.time() - _models[0] < 3600 and _models[1]:
        return _models[1]
    try:
        ids = await brain.list_models()
    except Exception:
        ids = []
    _models = (time.time(), ids)
    return ids


async def candidates() -> list[str]:
    """Which models to try for an image, in order."""
    s = config.store.load()
    if s.vision_model:
        return [s.vision_model]
    out = [] if s.model in _blind else [s.model]
    ids = await _available()
    pref = [m for m in PREFERRED.get(s.provider, []) if m in ids or not ids]
    guess = [m for m in ids if any(w in m.lower() for w in VISION_WORDS) and "embed" not in m.lower()]
    for m in pref + guess:
        if m not in out and m not in _blind:
            out.append(m)
    return out[:4]


def _refused(e: Exception) -> bool:
    low = str(e).lower()
    return getattr(e, "kind", "") in ("error", "model", "tools_unsupported") or any(
        w in low for w in ("image", "vision", "multimodal", "image_url", "content type", "invalid request"))


async def ask(images: list[np.ndarray], prompt: str, system: str = "", max_tokens: int = 600,
              max_side: int = 1024) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64(img, max_side)}"}})
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    last: Exception | None = None
    import asyncio

    for model in await candidates():
        try:
            for attempt in range(3):             # a busy vision server ("503 request limit reached") clears in seconds
                try:
                    text, _ = await brain.complete(messages, max_tokens=max_tokens, temperature=0.2, model=model)
                    break
                except BrainError as e:
                    if attempt == 2 or not (e.kind == "server" or "503" in str(e) or "502" in str(e)):
                        raise
                    log.info("%s is busy (%s); retrying", model, str(e)[:60])
                    await asyncio.sleep(2 + 3 * attempt)
            if text.strip():
                return text.strip()
        except BrainError as e:
            if e.kind == "server" or "503" in str(e):
                last = e
                continue                         # still busy: the next vision model

            if e.kind in ("auth", "rate_limit", "connection"):
                raise
            if not _refused(e):
                raise
            log.info("%s can't look at images (%s); trying the next vision model", model, str(e)[:80])
            _blind.add(model)
            last = e
    raise NoVision("No model here can look at images. Pick a vision model in Settings → Brain → Vision model "
                   "(e.g. a *-VL / vision / omni model)." + (f" ({last})" if last else ""))
