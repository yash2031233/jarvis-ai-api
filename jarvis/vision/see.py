"""Ask the model about images (needs a vision-capable model: local Qwen-VL, Gemma 3, NVIDIA VL models...)."""

from __future__ import annotations

import base64
from typing import Any

import numpy as np

from ..brain.client import BrainError, brain


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


async def ask(images: list[np.ndarray], prompt: str, system: str = "", max_tokens: int = 600,
              max_side: int = 1024) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64(img, max_side)}"}})
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    try:
        text, _ = await brain.complete(messages, max_tokens=max_tokens, temperature=0.2)
    except BrainError as e:
        low = str(e).lower()
        if any(w in low for w in ("image", "vision", "multimodal", "image_url", "content type")):
            raise NoVision("The current model can't look at images. Pick a vision model (e.g. a *-VL model) "
                           "in Settings.") from e
        raise
    return text.strip()
