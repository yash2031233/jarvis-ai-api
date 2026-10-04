"""Text recognition without a vision model.

Windows: the OCR engine built into Windows (Windows.Media.Ocr) in one long-running PowerShell process
— ~0.25 s for a whole 1440p screen, nothing to download. Elsewhere: RapidOCR (pip install
rapidocr-onnxruntime) or Tesseract (pytesseract) when installed.

Result: list of lines, each {"text", "x", "y", "w", "h", "words": [{"t", "x", "y", "w", "h"}]} in pixels
of the image that was read.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)
_PS1 = Path(__file__).resolve().parent / "winocr.ps1"


class OCRUnavailable(RuntimeError):
    pass


class _WinOCR:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_PS1)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=0x08000000,
        )
        ready = self.proc.stdout.readline()
        if '"ready"' not in ready:
            raise OCRUnavailable("Windows OCR didn't start")

    def read(self, path: Path) -> dict[str, Any]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            assert self.proc and self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(str(path) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            self.proc = None
            raise OCRUnavailable("Windows OCR stopped")
        return json.loads(line)


_win = _WinOCR() if sys.platform == "win32" else None


def _to_lines_win(res: dict[str, Any]) -> list[dict[str, Any]]:
    if "error" in res:
        raise OCRUnavailable(res["error"])
    out = []
    for ln in res.get("lines") or []:
        words = ln.get("words") or []
        if isinstance(words, dict):  # PowerShell collapses one-element arrays
            words = [words]
        if not words:
            continue
        x0 = min(w["x"] for w in words)
        y0 = min(w["y"] for w in words)
        x1 = max(w["x"] + w["w"] for w in words)
        y1 = max(w["y"] + w["h"] for w in words)
        out.append({"text": ln.get("text", ""), "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "words": words})
    return out


def _rapid(img: np.ndarray) -> list[dict[str, Any]]:
    from rapidocr_onnxruntime import RapidOCR  # optional

    eng = getattr(_rapid, "eng", None) or RapidOCR()
    _rapid.eng = eng  # type: ignore[attr-defined]
    res, _ = eng(img)
    out = []
    for box, text, _conf in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x, y, w, h = int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))
        out.append({"text": text, "x": x, "y": y, "w": w, "h": h, "words": [{"t": text, "x": x, "y": y, "w": w, "h": h}]})
    return out


def _tesseract(img: np.ndarray) -> list[dict[str, Any]]:
    import pytesseract  # optional

    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    lines: dict[tuple, dict] = {}
    for i, t in enumerate(d["text"]):
        if not t.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        w = {"t": t, "x": d["left"][i], "y": d["top"][i], "w": d["width"][i], "h": d["height"][i]}
        lines.setdefault(key, {"words": []})["words"].append(w)
    out = []
    for ln in lines.values():
        ws = ln["words"]
        x0, y0 = min(w["x"] for w in ws), min(w["y"] for w in ws)
        x1, y1 = max(w["x"] + w["w"] for w in ws), max(w["y"] + w["h"] for w in ws)
        out.append({"text": " ".join(w["t"] for w in ws), "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "words": ws})
    return out


def read_image(img: np.ndarray) -> list[dict[str, Any]]:
    """OCR an RGB/BGR uint8 image. Returns text lines with boxes."""
    if _win is not None:
        import cv2

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            p = Path(f.name)
        try:
            cv2.imwrite(str(p), img)
            return _to_lines_win(_win.read(p))
        finally:
            p.unlink(missing_ok=True)
    for engine in (_rapid, _tesseract):
        try:
            return engine(img)
        except ImportError:
            continue
    raise OCRUnavailable("No OCR engine available. Install one: pip install rapidocr-onnxruntime")


def text_of(lines: list[dict[str, Any]]) -> str:
    """Lines in reading order (top to bottom, then left to right)."""
    rows = sorted(lines, key=lambda ln: (round(ln["y"] / max(8, ln["h"] or 8)), ln["x"]))
    return "\n".join(ln["text"] for ln in rows)


def find(lines: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Where a piece of text is: exact phrase first, then fuzzy. Returns boxes with centres, best first."""
    from rapidfuzz import fuzz

    q = query.strip().lower()
    hits = []
    for ln in lines:
        t = ln["text"].lower()
        if q in t:
            # narrow to the matching words when possible
            words = ln["words"]
            for i in range(len(words)):
                acc = ""
                for j in range(i, len(words)):
                    acc = (acc + " " + words[j]["t"]).strip().lower()
                    if q in acc:
                        sel = words[i:j + 1]
                        x0, y0 = min(w["x"] for w in sel), min(w["y"] for w in sel)
                        x1, y1 = max(w["x"] + w["w"] for w in sel), max(w["y"] + w["h"] for w in sel)
                        # exact short labels beat the same words buried in a long line (URL bars, sentences)
                        score = 100 - min(30, (len(t) - len(q)) * 0.5)
                        hits.append({"text": " ".join(w["t"] for w in sel), "score": round(score, 1), "x": (x0 + x1) // 2,
                                     "y": (y0 + y1) // 2, "box": [x0, y0, x1 - x0, y1 - y0]})
                        break
                else:
                    continue
                break
        else:
            s = fuzz.partial_ratio(q, t)
            if s >= 80:
                hits.append({"text": ln["text"], "score": s, "x": ln["x"] + ln["w"] // 2, "y": ln["y"] + ln["h"] // 2,
                             "box": [ln["x"], ln["y"], ln["w"], ln["h"]]})
    hits.sort(key=lambda h: -h["score"])
    return hits
