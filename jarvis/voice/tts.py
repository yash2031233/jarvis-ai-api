"""Text-to-speech with Kokoro (local). Sentence queue → gapless playback, interruptible.

Emits the speaking amplitude ~30x/sec so the orb can pulse with the voice.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from ..events import bus

log = logging.getLogger(__name__)

VOICES = ["bm_george", "bm_lewis", "bm_daniel", "bm_fable", "am_michael", "am_adam", "am_onyx",
          "bf_emma", "bf_isabella", "af_heart", "af_bella", "af_nicole", "af_sky"]


class TTS:
    def __init__(self) -> None:
        self.kokoro = None
        self.q: queue.Queue[str | None] = queue.Queue()
        self.stop_flag = threading.Event()
        self.speaking = threading.Event()
        self.voice = "bm_george"
        self.speed = 1.0
        self.output_device: int | None = None
        self._worker: threading.Thread | None = None
        self.on_idle = None  # callback when the queue drains

    def load(self) -> None:
        from kokoro_onnx import Kokoro

        from .models import kokoro_paths

        m, v = kokoro_paths()
        t0 = time.time()
        self.kokoro = Kokoro(str(m), str(v))
        self.kokoro.create("Ready.", voice=self.voice, speed=self.speed)  # warm-up
        log.info("TTS ready (kokoro, %.1fs)", time.time() - t0)
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True, name="tts")
            self._worker.start()

    def voices(self) -> list[str]:
        if self.kokoro is not None:
            try:
                return sorted(self.kokoro.get_voices())
            except Exception:
                pass
        return VOICES

    def say(self, text: str) -> None:
        if self.kokoro is None:
            return
        self.stop_flag.clear()
        self.q.put(text)

    def stop(self) -> None:
        """Barge-in: drop everything queued and cut the current sentence."""
        self.stop_flag.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def is_busy(self) -> bool:
        return self.speaking.is_set() or not self.q.empty()

    def _run(self) -> None:
        import sounddevice as sd

        while True:
            text = self.q.get()
            if text is None:
                return
            if self.stop_flag.is_set():
                continue
            try:
                samples, sr = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang="en-us")
            except Exception as e:
                log.warning("tts failed for %r: %s", text[:40], e)
                continue
            if self.stop_flag.is_set():
                continue
            self.speaking.set()
            bus.emit("orb", state="speaking")
            try:
                self._play(sd, samples.astype(np.float32), sr)
            except Exception as e:
                log.warning("audio playback failed: %s", e)
            self.speaking.clear()
            if self.q.empty():
                bus.emit("level", source="tts", value=0.0)
                if self.on_idle:
                    self.on_idle()

    def _play(self, sd, samples: np.ndarray, sr: int) -> None:
        block = int(sr / 30)
        pos = 0
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", device=self.output_device,
                             blocksize=block) as out:
            while pos < len(samples):
                if self.stop_flag.is_set():
                    break
                chunk = samples[pos:pos + block]
                out.write(chunk.reshape(-1, 1))
                rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
                bus.emit("level", source="tts", value=min(1.0, rms * 6))
                pos += block
