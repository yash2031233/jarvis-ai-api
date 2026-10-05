"""MEDIA tags: how Jarvis attaches pictures, videos and files to a reply (the same convention as Jarvis v1).

A line like `MEDIA:/api/camera/snap/x.jpg` or `MEDIA:C:\\path\\to\\file.png` in a reply is
  * sent as the actual photo / video / file on Telegram (with the rest of the text as the message),
  * shown inline in the app (images) or as a link (other files),
  * never read aloud.
Paths come from tool results: camera and car snapshots, screenshots, diagrams, 3D part previews, recordings, files.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config

TAG = re.compile(r"MEDIA:\s*(\"[^\"]+\"|'[^']+'|\S+)")
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO = {".mp4", ".mov", ".webm"}


def split(text: str) -> tuple[str, list[str]]:
    """Reply text without its MEDIA tags, and the tags' references (in order, no repeats)."""
    refs: list[str] = []
    for m in TAG.finditer(text or ""):
        r = m.group(1).strip("\"'").rstrip(".,;)")
        if r and r not in refs:
            refs.append(r)
    clean = re.sub(r"[ \t]*" + TAG.pattern + r"[ \t]*\n?", "", text or "").strip()
    return clean, refs


def resolve(ref: str) -> Path | None:
    """A MEDIA reference -> the local file, or None if it isn't one Jarvis can send."""
    ref = ref.split("?", 1)[0]
    d = config.DATA_DIR
    m = re.match(r"^/api/camera/snap/([\w.\-]+)$", ref)
    if m:
        p = d / "camera" / m.group(1)
    elif re.match(r"^/api/media/[\w.\-]+$", ref):
        p = d / "media" / ref.rsplit("/", 1)[1]
    elif (m := re.match(r"^/api/cad/file/([\w\-]+)/(\d+)/([\w.\-]+)$", ref)):
        p = d / "cad" / m.group(1) / f"v{int(m.group(2))}" / m.group(3)
    else:
        p = Path(ref).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_file() and p.stat().st_size < 50_000_000 else None


def kind(p: Path) -> str:
    s = p.suffix.lower()
    return "image" if s in IMAGE else "video" if s in VIDEO else "file"
