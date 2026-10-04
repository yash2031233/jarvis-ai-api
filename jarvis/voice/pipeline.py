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


def list_output_devices() -> list[str]:
    """Speakers / headphones, by name (indices change when devices come and go), one entry per device."""
    try:
        import sounddevice as sd

        host = sd.default.hostapi
        names = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0 and d["hostapi"] == host]
        return list(dict.fromkeys(names))
    except Exception:
        return []


def output_index(name: str) -> int | None:
    """The device index for a speaker name; None = the system default (also when it's unplugged)."""
    if not name:
        return None
    try:
        import sounddevice as sd

        host = sd.default.hostapi
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and d["name"] == name and d["hostapi"] == host:
                return i
    except Exception:
        pass
    return None


def match_wake(text: str, phrase: str) -> str | None:
    """'Jarvis, open Spotify' -> 'open Spotify'; 'Jarvis?' -> ''; anything not starting with the wake phrase -> None.
    The phrase has to come first (or after "hey"/"ok"), so "I told Jarvis…" in a conversation doesn't wake him, and
    small transcription slips count ("Jarvis" / "Jervis" / "Jarvis's")."""
    import re

    from rapidfuzz import fuzz

    phrase = phrase.strip().lower()
    if not phrase or not text:
        return None
    n = len(phrase.split())
    words = list(re.finditer(r"[A-Za-z']+", text))
    starts = [0]
    if words and words[0].group(0).lower() in ("hey", "hi", "ok", "okay", "yo", "oh", "um", "uh", "so"):
        starts.append(1)
    for i in starts:
        group = words[i:i + n]
        if len(group) < n:
            break
        said = " ".join(w.group(0).lower() for w in group).removesuffix("'s")
        if fuzz.ratio(said, phrase) >= 80:
            return text[group[-1].end():].lstrip(" ,.!?:;-").strip()
    return None


class VoicePipeline:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.stt = STT()
        self.tts = TTS()
        self.wake = None
        self._wake_phrase = ""
        self._spotting = False
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
            self.tts.output_device = output_index(s.speaker_device)
            self.tts.on_idle = lambda: self._after_speech()
            self.tts.load()
            self.stt.load(s.voice_engine)
            if s.wake_word:
                self._wake_phrase = (s.wake_phrase or "").strip()
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

        spot: list[np.ndarray] | None = None   # an utterance being collected to check for the wake phrase
        spot_sil = 0
        spot_t0 = 0.0

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
                # "Jarvis …": speech in the room is transcribed a phrase at a time (fast on a GPU) and checked
                if not woke and self._wake_phrase and not self._spotting:
                    if spot is None:
                        if rms > max(0.015, noise * 3.5):
                            spot, spot_sil, spot_t0 = list(pre_roll), 0, now
                    else:
                        spot.append(frame)
                        spot_sil = 0 if rms > max(0.012, noise * 3.0) else spot_sil + 80
                        if spot_sil >= 560 or now - spot_t0 > 6:
                            audio, spot = np.concatenate(spot), None
                            if len(audio) > SR * 0.35:
                                self._spotting = True
                                threading.Thread(target=self._spot, args=(audio,), daemon=True).start()
                if woke:
                    spot = None
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

    def _spot(self, audio: np.ndarray) -> None:
        try:
            text = self.stt.transcribe(audio, prompt="Jarvis")
            rest = match_wake(text, self._wake_phrase)
            if rest is None or self.mode != "idle":
                return
            if len(rest.split()) >= 2:            # "Jarvis, open Spotify": the request came with the name
                self.interrupt()
                self.mode = "processing"
                orb_state("thinking")
                bus.emit("transcript", text=rest, ms=0)
                threading.Thread(target=self._process_text, args=(rest,), daemon=True).start()
            else:                                 # just "Jarvis": listen for the request
                self._ptt.set()
        except Exception as e:
            log.debug("wake phrase check failed: %s", e)
        finally:
            self._spotting = False

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
        self._process_text(text)

    def _process_text(self, text: str) -> None:
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
