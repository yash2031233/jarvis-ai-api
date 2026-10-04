"""Voice loop: mic → wake word / push-to-talk → VAD → Whisper → agent → Kokoro.

Barge-in: saying "Hey Jarvis", pressing the hotkey/mic button, or typing while Jarvis
is talking stops speech immediately and starts listening.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
from collections import deque

import numpy as np

from .. import config
from ..events import bus, orb_state
from .stt import STT
from .tts import TTS

log = logging.getLogger(__name__)

SR = 16000
FRAME = 1280  # 80 ms — what openWakeWord expects


def pick_input_device(preferred: int | None) -> tuple[int | None, int]:
    """Return (device index, native samplerate). Handles machines with no default input."""
    import sounddevice as sd

    devs = sd.query_devices()
    if preferred is not None and 0 <= preferred < len(devs) and devs[preferred]["max_input_channels"] > 0:
        return preferred, int(devs[preferred]["default_samplerate"])
    try:
        d = sd.query_devices(kind="input")
        return None, int(d["default_samplerate"])
    except Exception:
        pass
    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    rank = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Core Audio": 0, "ALSA": 1,
            "PulseAudio": 0, "Windows WDM-KS": 5}
    cands = [(rank.get(apis.get(d["hostapi"], ""), 3), i, d) for i, d in enumerate(devs)
             if d["max_input_channels"] > 0]
    if not cands:
        raise RuntimeError("No microphone found. Check that a mic is connected and that "
                           "microphone access for desktop apps is allowed in your OS privacy settings.")
    cands.sort(key=lambda c: (c[0], "line" in c[2]["name"].lower()))
    _, idx, d = cands[0]
    return idx, int(d["default_samplerate"])


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd

        return [{"index": i, "name": d["name"]} for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]
    except Exception:
        return []


class VoicePipeline:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.stt = STT()
        self.tts = TTS()
        self.wake = None
        self.ready = False
        self.status = "off"
        self.error = ""
        self.mode = "idle"  # idle | listening | processing
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._ptt = threading.Event()
        self._stop = threading.Event()
        self._stream = None
        self._thread: threading.Thread | None = None
        self.device_sr = SR
        self.device: int | None = None
        self._asked: set[str] = set()

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        threading.Thread(target=self._boot, daemon=True, name="voice-boot").start()

    def _set_status(self, status: str, error: str = "") -> None:
        self.status, self.error = status, error
        bus.emit("voice_status", status=status, error=error, stt=self.stt.name, stt_device=self.stt.device)

    def _boot(self) -> None:
        s = config.store.load()
        try:
            self._set_status("loading")
            self.tts.voice, self.tts.speed = s.tts_voice, s.tts_speed
            self.tts.on_idle = lambda: self._after_speech()
            self.tts.load()
            self.stt.load(s.voice_engine)
            if s.wake_word:
                try:
                    self._load_wake()
                except Exception as e:
                    log.warning("wake word unavailable: %s", e)
                    bus.emit("notice", level="warn", text=f"Wake word unavailable ({e}). Use the mic button.")
            self.ready = True
            self._open_mic()
            self._set_status("ready")
        except Exception as e:
            log.exception("voice failed to start")
            self._set_status("error", str(e))
            # TTS may still work without a mic
            self.ready = self.tts.kokoro is not None

    def _load_wake(self) -> None:
        from openwakeword.model import Model

        from .models import wakeword_paths

        d = wakeword_paths()
        model_file = sorted(d.glob("hey_jarvis*.onnx"))[0]
        self.wake = Model(wakeword_models=[str(model_file)], inference_framework="onnx",
                          melspec_model_path=str(next(d.glob("melspectrogram*.onnx"))),
                          embedding_model_path=str(next(d.glob("embedding_model*.onnx"))))

    def _open_mic(self) -> None:
        import sounddevice as sd

        s = config.store.load()
        self.device, self.device_sr = pick_input_device(s.mic_device)
        blocksize = int(self.device_sr * FRAME / SR)

        def cb(indata, frames, t, status):
            mono = indata[:, 0] if indata.ndim > 1 else indata
            try:
                self._frames.put_nowait(mono.copy())
            except queue.Full:
                pass

        self._stream = sd.InputStream(samplerate=self.device_sr, channels=1, dtype="float32",
                                      blocksize=blocksize, device=self.device, callback=cb)
        self._stream.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice-loop")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.tts.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()

    # ------------------------------------------------------------------ control
    def push_to_talk(self) -> None:
        """Mic button / hotkey: stop talking and start listening now."""
        self.interrupt()
        self._ptt.set()

    def interrupt(self) -> None:
        if self.tts.is_busy():
            self.tts.stop()
            bus.emit("barge_in")

    def speak(self, sentence: str) -> None:
        self.tts.say(sentence)

    def _after_speech(self) -> None:
        if self.mode == "idle":
            bus.emit("orb", state="idle")

    # ------------------------------------------------------------------ main loop
    def _resample(self, x: np.ndarray) -> np.ndarray:
        if self.device_sr == SR:
            return x
        n = int(len(x) * SR / self.device_sr)
        return np.interp(np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x).astype(np.float32)

    def _loop(self) -> None:
        noise = 0.004
        pre_roll: deque[np.ndarray] = deque(maxlen=6)  # ~0.5 s before speech starts
        speech: list[np.ndarray] = []
        heard_voice = False
        silence_ms = 0
        listen_started = 0.0
        last_level_emit = 0.0

        while not self._stop.is_set():
            try:
                raw = self._frames.get(timeout=0.5)
            except queue.Empty:
                continue
            frame = self._resample(raw)
            rms = float(np.sqrt(np.mean(frame ** 2)) + 1e-9)
            now = time.time()
            if now - last_level_emit > 0.033:
                bus.emit("level", source="mic", value=min(1.0, rms * 12))
                last_level_emit = now

            # ---- idle: wake word / push-to-talk
            if self.mode == "idle":
                noise = 0.995 * noise + 0.005 * min(rms, 0.05)
                pre_roll.append(frame)
                woke = False
                if self._ptt.is_set():
                    self._ptt.clear()
                    woke = True
                elif self.wake is not None:
                    pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16)
                    scores = self.wake.predict(pcm)
                    if max(scores.values(), default=0) > 0.5:
                        woke = True
                        self.wake.reset()
                if woke:
                    self.interrupt()
                    self.mode = "listening"
                    speech = []
                    heard_voice = False
                    silence_ms = 0
                    listen_started = now
                    orb_state("listening")
                    bus.emit("listening", on=True)
                continue

            # ---- listening: collect until end of speech
            if self.mode == "listening":
                speech.append(frame)
                threshold = max(0.012, noise * 3.0)
                if rms > threshold:
                    heard_voice = True
                    silence_ms = 0
                else:
                    silence_ms += 80
                elapsed = now - listen_started
                end = (heard_voice and silence_ms >= 700) or elapsed > 15 or (not heard_voice and elapsed > 5)
                if end:
                    bus.emit("listening", on=False)
                    if not heard_voice:
                        self.mode = "idle"
                        orb_state("idle")
                        continue
                    self.mode = "processing"
                    orb_state("thinking")
                    audio = np.concatenate(speech)
                    threading.Thread(target=self._process, args=(audio,), daemon=True).start()
                continue
            # processing: if the agent is waiting for a yes/no, listen for it (once per request)
            if self.mode == "processing" and not self.tts.is_busy():
                from ..safety.permissions import confirmations

                pending = set(confirmations.pending)
                if pending and not pending <= self._asked:
                    self._asked |= pending
                    self.mode = "listening"
                    speech, heard_voice, silence_ms, listen_started = [], False, 0, now
                    bus.emit("listening", on=True)
                    continue
            # processing: ignore mic, but still allow wake-word barge-in
            if self.mode == "processing" and self.wake is not None and self.tts.is_busy():
                pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16)
                if max(self.wake.predict(pcm).values(), default=0) > 0.6:
                    self.wake.reset()
                    self.interrupt()
                    asyncio.run_coroutine_threadsafe(self._cancel_agent(), self.loop)
                    self.mode = "idle"
                    self._ptt.set()

    async def _cancel_agent(self) -> None:
        from ..agent.engine import agent

        agent.cancel()

    def _process(self, audio: np.ndarray) -> None:
        t0 = time.time()
        try:
            text = self.stt.transcribe(audio)
        except Exception as e:
            log.warning("transcription failed: %s", e)
            text = ""
        bus.emit("transcript", text=text, ms=int((time.time() - t0) * 1000))
        if not text:
            self.mode = "idle"
            orb_state("idle")
            return
        fut = asyncio.run_coroutine_threadsafe(self._dispatch(text), self.loop)
        try:
            fut.result(timeout=600)
        except Exception as e:
            log.warning("voice dispatch error: %s", e)
        from ..safety.permissions import confirmations

        if confirmations.pending:
            return  # the original request is still waiting on this answer
        self.mode = "idle"
        if not self.tts.is_busy():
            orb_state("idle")

    async def _dispatch(self, text: str) -> None:
        from ..agent.engine import agent
        from ..safety.permissions import confirmations

        low = text.lower().strip(" .!?")
        if confirmations.pending:
            if re.match(r"^(yes|yeah|yep|sure|do it|go ahead|confirm|approved?|ok(ay)?)\b", low):
                confirmations.answer_latest(True)
                return
            if re.match(r"^(no|nope|don'?t|cancel|stop|deny|never ?mind)\b", low):
                confirmations.answer_latest(False)
                return
        if low in {"stop", "cancel", "never mind", "nevermind", "shut up", "quiet", "be quiet"}:
            agent.cancel()
            self.tts.stop()
            orb_state("idle")
            return
        await agent.handle(text, source="voice")
