"""Speech-to-text with faster-whisper. GPU when available, automatic CPU fallback."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ..hardware import voice_profile
from .models import MODELS_DIR, add_cuda_dll_dirs

log = logging.getLogger(__name__)


class STT:
    def __init__(self) -> None:
        self.model = None
        self.device = ""
        self.name = ""
        self._lock = threading.Lock()

    def load(self, engine: str = "auto") -> None:
        from faster_whisper import WhisperModel

        prof = voice_profile(engine)
        add_cuda_dll_dirs()
        attempts = [(prof["stt_model"], prof["stt_device"], prof["stt_compute"])]
        if prof["stt_device"] == "cuda":
            attempts.append(("small.en", "cpu", "int8"))
        last_err = None
        for name, device, compute in attempts:
            try:
                t0 = time.time()
                m = WhisperModel(name, device=device, compute_type=compute,
                                 download_root=str(MODELS_DIR / "whisper"))
                # warm-up (also surfaces missing cuDNN/cuBLAS DLLs right away)
                list(m.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
                self.model, self.device, self.name = m, device, name
                log.info("STT ready: %s on %s (%.1fs)", name, device, time.time() - t0)
                return
            except Exception as e:
                last_err = e
                log.warning("STT %s/%s failed: %s", name, device, e)
        raise RuntimeError(f"Speech recognition failed to load: {last_err}")

    def transcribe(self, audio: np.ndarray) -> str:
        if self.model is None:
            raise RuntimeError("STT not loaded")
        with self._lock:
            segments, _info = self.model.transcribe(
                audio.astype(np.float32), language="en", beam_size=1 if self.device == "cpu" else 3,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False, without_timestamps=True,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        # Whisper hallucinations on silence
        if text.lower().strip(" .!") in {"", "you", "thank you", "thanks for watching", "bye"}:
            return ""
        return text
