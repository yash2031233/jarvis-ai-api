"""Hardware detection → picks the voice engine profile."""

from __future__ import annotations

import functools
import logging
import platform
import shutil
import subprocess

log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def detect() -> dict:
    info: dict = {"os": platform.system(), "arch": platform.machine(), "cuda": False, "gpu_name": "",
                  "vram_gb": 0, "compute_capability": "", "blackwell": False, "apple_silicon": False}
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        info["apple_silicon"] = True
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=0x08000000 if platform.system() == "Windows" else 0,
            ).stdout.strip().splitlines()
            if out:
                parts = [p.strip() for p in out[0].split(",")]
                info["gpu_name"] = parts[0]
                info["vram_gb"] = round(int(parts[1]) / 1024) if len(parts) > 1 and parts[1].isdigit() else 0
                cc = parts[2] if len(parts) > 2 else ""
                info["compute_capability"] = cc
                try:
                    info["blackwell"] = float(cc) >= 10.0
                except ValueError:
                    pass
        except Exception as e:
            log.debug("nvidia-smi failed: %s", e)
    # Is CUDA actually usable by CTranslate2 (faster-whisper)?
    try:
        import ctranslate2

        info["cuda"] = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        info["cuda"] = False
    return info


def voice_profile(engine: str = "auto") -> dict:
    """Decide STT/TTS models + devices for the requested engine."""
    hw = detect()
    use_gpu = engine == "gpu" or (engine == "auto" and hw["cuda"])
    if use_gpu and hw["cuda"]:
        return {"stt_model": "large-v3-turbo", "stt_device": "cuda", "stt_compute": "float16",
                "tts": "kokoro", "label": f"GPU ({hw['gpu_name']})"}
    big_cpu = (__import__("os").cpu_count() or 4) >= 8
    return {"stt_model": "small.en" if big_cpu else "base.en", "stt_device": "cpu", "stt_compute": "int8",
            "tts": "kokoro" if big_cpu else "piper", "label": "CPU"}
