"""First-run model downloads with progress events."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import httpx

from .. import config
from ..events import bus

log = logging.getLogger(__name__)

MODELS_DIR = config.DATA_DIR / "models"

KOKORO_FILES = {
    "kokoro-v1.0.onnx":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def download(url: str, dest: Path, label: str) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    bus.emit("download", label=label, progress=0.0)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) or None
        done = 0
        last = -1.0
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    p = round(done / total, 2)
                    if p != last:
                        bus.emit("download", label=label, progress=p)
                        last = p
    tmp.replace(dest)
    bus.emit("download", label=label, progress=1.0)
    return dest


def kokoro_paths() -> tuple[Path, Path]:
    d = MODELS_DIR / "kokoro"
    m = download(KOKORO_FILES["kokoro-v1.0.onnx"], d / "kokoro-v1.0.onnx", "Voice model (Kokoro)")
    v = download(KOKORO_FILES["voices-v1.0.bin"], d / "voices-v1.0.bin", "Voice styles")
    return m, v


def wakeword_paths() -> Path:
    d = MODELS_DIR / "openwakeword"
    d.mkdir(parents=True, exist_ok=True)
    if not list(d.glob("hey_jarvis*.onnx")):
        bus.emit("download", label="Wake word model", progress=0.0)
        from openwakeword.utils import download_models

        download_models(["hey_jarvis"], target_directory=str(d))
        bus.emit("download", label="Wake word model", progress=1.0)
    return d


def add_cuda_dll_dirs() -> None:
    """Windows: make pip-installed CUDA libs (nvidia-cublas-cu12 / nvidia-cudnn-cu12) loadable."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # type: ignore

        base = Path(list(nvidia.__path__)[0])
    except Exception:
        return
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        b = base / sub / "bin"
        if b.exists():
            os.add_dll_directory(str(b))
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
