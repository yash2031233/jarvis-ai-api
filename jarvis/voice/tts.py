"""Text-to-speech, local. Sentence queue -> streamed, interruptible playback.

Engines:
  kokoro   Kokoro-82M via ONNX (default, always available with the voice extras) - fast on CPU
  pocket   Kyutai Pocket TTS (optional: pip install pocket-tts torch) - streams, so the first audio starts
           ~0.1-0.3 s after a sentence begins; warmer voices. Voice ids look like "pocket:peter_yearsley".
If Pocket TTS isn't installed or fails, Jarvis falls back to Kokoro by himself.

Emits the speaking amplitude ~30x/sec so the orb can pulse with the voice.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Iterator

import numpy as np

from ..events import bus

log = logging.getLogger(__name__)

VOICES = ["bm_george", "bm_lewis", "bm_daniel", "bm_fable", "am_michael", "am_adam", "am_onyx",
          "bf_emma", "bf_isabella", "af_heart", "af_bella", "af_nicole", "af_sky"]
POCKET_VOICES = {
    "pocket:peter_yearsley": "Peter (Pocket) - British, warm",
    "pocket:stuart_bell": "Stuart (Pocket) - British",
    "pocket:charles": "Charles (Pocket) - southern English",
    "pocket:paul": "Paul (Pocket) - English",
}


def pocket_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("pocket_tts") is not None and importlib.util.find_spec("torch") is not None
    except Exception:
        return False


class TTS:
    def __init__(self) -> None:
        self.kokoro = None
        self.pocket = None
        self._pocket_states: dict[str, object] = {}
        self._pocket_failed = False
        self.q: queue.Queue[str | None] = queue.Queue()
        self.stop_flag = threading.Event()
        self.speaking = threading.Event()
        self.voice = "bm_george"
        self.speed = 1.0
        self.output_device: int | None = None
        self._worker: threading.Thread | None = None
        self.on_idle = None  # callback when the queue drains
        self.last_first_audio_ms: int | None = None
        self._working = False   # a sentence is being synthesized (not playing yet, but not idle either)

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        from kokoro_onnx import Kokoro

        from .models import kokoro_paths

        m, v = kokoro_paths()
        t0 = time.time()
        self.kokoro = Kokoro(str(m), str(v))
        self.kokoro.create("Ready.", voice=self._kokoro_voice(), speed=self.speed)  # warm-up
        log.info("TTS ready (kokoro, %.1fs)", time.time() - t0)
        if self.voice.startswith("pocket:"):
            self._load_pocket()
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, daemon=True, name="tts")
            self._worker.start()

    def _load_pocket(self) -> bool:
        if self.pocket is not None:
            return True
        if self._pocket_failed or not pocket_available():
            return False
        try:
            import torch
            from pocket_tts import TTSModel

            t0 = time.time()
            m = TTSModel.load_model()
            if torch.cuda.is_available():
                m = m.to("cuda")
            self.pocket = m
            for _ in self._pocket_chunks("Ready.", self.voice):  # warm-up + voice state
                pass
            log.info("TTS ready (pocket, %s, %.1fs)", "cuda" if torch.cuda.is_available() else "cpu", time.time() - t0)
            return True
        except Exception as e:
            self._pocket_failed = True
            log.warning("Pocket TTS unavailable, using Kokoro: %s", e)
            return False

    def _kokoro_voice(self) -> str:
        return self.voice if not self.voice.startswith("pocket:") else "bm_george"

    def voices(self) -> list[str]:
        out = VOICES
        if self.kokoro is not None:
            try:
                out = sorted(self.kokoro.get_voices())
            except Exception:
                pass
        return (list(POCKET_VOICES) if pocket_available() else []) + list(out)

    # ------------------------------------------------------------------ engines -> chunks
    def _pocket_chunks(self, text: str, voice: str) -> Iterator[tuple[np.ndarray, int]]:
        name = voice.split(":", 1)[1]
        st = self._pocket_states.get(name)
        if st is None:
            st = self._pocket_states[name] = self.pocket.get_state_for_audio_prompt(name)
        for ch in self.pocket.generate_audio_stream(st, text):
            yield ch.squeeze().float().cpu().numpy().astype(np.float32), int(self.pocket.sample_rate)

    def _chunks(self, text: str) -> Iterator[tuple[np.ndarray, int]]:
        if self.voice.startswith("pocket:") and self._load_pocket():
            try:
                yield from self._pocket_chunks(text, self.voice)
                return
            except Exception as e:
                log.warning("pocket tts failed (%s); falling back to kokoro", e)
        samples, sr = self.kokoro.create(text, voice=self._kokoro_voice(), speed=self.speed, lang="en-us")
        yield samples.astype(np.float32), sr

    # ------------------------------------------------------------------ queue
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
        return self._working or self.speaking.is_set() or not self.q.empty()

    def _run(self) -> None:
        import sounddevice as sd

        while True:
            text = self.q.get()
            if text is None:
                return
            if self.stop_flag.is_set():
                continue
            self._working = True
            t0 = time.time()
            out = None
            sr_open = None
            try:
                for chunk, sr in self._chunks(text):
                    if self.stop_flag.is_set():
                        break
                    if out is None or sr != sr_open:
                        if out is not None:
                            out.close()
                        out = sd.OutputStream(samplerate=sr, channels=1, dtype="float32", device=self.output_device,
                                              blocksize=int(sr / 30))
                        out.start()
                        sr_open = sr
                        self.last_first_audio_ms = int((time.time() - t0) * 1000)
                        self.speaking.set()
                        bus.emit("orb", state="speaking")
                    self._play(out, chunk, sr)
            except Exception as e:
                log.warning("tts failed for %r: %s", text[:40], e)
            finally:
                if out is not None:
                    try:
                        out.stop()
                        out.close()
                    except Exception:
                        pass
            self.speaking.clear()
            self._working = False
            if self.q.empty():
                bus.emit("level", source="tts", value=0.0)
                if self.on_idle:
                    self.on_idle()

    def _play(self, out, samples: np.ndarray, sr: int) -> None:
        block = int(sr / 30)
        pos = 0
        while pos < len(samples):
            if self.stop_flag.is_set():
                break
            chunk = samples[pos:pos + block]
            out.write(chunk.reshape(-1, 1))
            rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
            bus.emit("level", source="tts", value=min(1.0, rms * 6))
            pos += block
