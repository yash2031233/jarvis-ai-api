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


def input_candidates() -> list[tuple[int, int]]:
    """Every microphone as (index, samplerate), best first: Windows' own default, then WASAPI/MME/Core Audio/Pulse
    devices; line-ins / stereo mix and kernel-streaming (WDM-KS) last."""
    import sounddevice as sd

    devs = sd.query_devices()
    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    rank = {"Windows WASAPI": 1, "MME": 1, "Windows DirectSound": 2, "Core Audio": 0, "ALSA": 1,
            "PulseAudio": 0, "Windows WDM-KS": 5}
    try:
        default = sd.query_devices(kind="input")["name"]
    except Exception:
        default = None
    cands = [("line" in d["name"].lower() or "stereo mix" in d["name"].lower(),
              0 if d["name"] == default else rank.get(apis.get(d["hostapi"], ""), 3), i, d)
             for i, d in enumerate(devs) if d["max_input_channels"] > 0]
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    return [(i, int(d["default_samplerate"])) for _, _, i, d in cands]


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
    cands = input_candidates()
    if not cands:
        raise RuntimeError("No microphone found. Check that a mic is connected and that "
                           "microphone access for desktop apps is allowed in your OS privacy settings.")
    return cands[0]


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
    """The device index for a speaker name. "System default" on Windows = the MME Sound Mapper, which always plays on
    the device Windows is using *now* (PortAudio's own default is frozen at startup, so switching from headphones to
    speakers later left Jarvis talking into the headphones). None = PortAudio's default."""
    if not name:
        try:
            import sounddevice as sd

            for i, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0 and d["name"].startswith("Microsoft Sound Mapper"):
                    return i
        except Exception:
            pass
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


def _dev_name(idx: int | None, kind: int) -> str:
    try:
        import sounddevice as sd

        return sd.query_devices(idx if idx is not None else sd.default.device[kind])["name"]
    except Exception:
        return "?"


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
        self._mic_t0: float | None = None
        self._carry = False
        self._carry_audio: np.ndarray | None = None
        self._mic_peak = 0.0
        self._mic_tried: set[int | None] = set()
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
            log.info("voice: mic = %s | speaker = %s | wake = %s%s", _dev_name(self.device, 0),
                     _dev_name(self.tts.output_device, 1), self._wake_phrase or "off",
                     " + hey jarvis model" if self.wake else "")
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

    def _open_mic(self, device: tuple[int | None, int] | None = None) -> None:
        import sounddevice as sd

        s = config.store.load()
        self.device, self.device_sr = device or pick_input_device(s.mic_device)
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
        self._mic_t0, self._mic_peak = time.time(), 0.0
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="voice-loop")
            self._thread.start()

    def _check_mic(self, rms: float) -> None:
        """A mic that delivers pure silence (wrong jack, a disabled device, Line In) is swapped for the next one."""
        if self._mic_t0 is None:
            return
        self._mic_peak = max(self._mic_peak, rms)
        if time.time() - self._mic_t0 < 8:
            return
        self._mic_t0 = None
        if self._mic_peak > 3e-4:
            return
        self._mic_tried.add(self.device)
        if config.store.load().mic_device is None:
            for idx, sr in input_candidates():
                if idx in self._mic_tried:
                    continue
                log.warning("voice: %s is silent, trying another mic", _dev_name(self.device, 0))
                try:
                    self._stream.stop()
                    self._stream.close()
                    self._open_mic((idx, sr))
                    return
                except Exception as e:
                    self._mic_tried.add(idx)
                    log.warning("voice: mic %s won't open: %s", idx, e)
        log.warning("voice: the microphone (%s) hears nothing", _dev_name(self.device, 0))
        bus.emit("notice", level="warn", text="Your microphone isn't picking anything up - pick the right one in "
                                              "Settings → Voice → Microphone (and check it isn't muted).")

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
        after_spot: list[np.ndarray] | None = None   # what was said while a phrase was being checked

        while not self._stop.is_set():
            try:
                raw = self._frames.get(timeout=0.5)
            except queue.Empty:
                continue
            frame = self._resample(raw)
            rms = float(np.sqrt(np.mean(frame ** 2)) + 1e-9)
            now = time.time()
            self._check_mic(rms)
            if now - last_level_emit > 0.033:
                bus.emit("level", source="mic", value=min(1.0, rms * 12))
                last_level_emit = now

            # ---- idle: wake word / push-to-talk
            if self.mode == "idle":
                noise = 0.995 * noise + 0.005 * min(rms, 0.05)
                pre_roll.append(frame)
                if after_spot is not None:
                    if self._spotting or self._carry:
                        after_spot.append(frame)
                        del after_spot[:-100]                 # 8 s is plenty
                    else:
                        after_spot = None
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
                        if rms > max(0.006, noise * 3.5):
                            spot, spot_sil, spot_t0 = list(pre_roll), 0, now
                    else:
                        spot.append(frame)
                        spot_sil = 0 if rms > max(0.005, noise * 3.0) else spot_sil + 80
                        if spot_sil >= 560 or now - spot_t0 > 6:
                            audio, spot = np.concatenate(spot), None
                            if len(audio) > SR * 0.35:
                                self._spotting = True
                                after_spot = []
                                threading.Thread(target=self._spot, args=(audio,), daemon=True).start()
                if woke:
                    spot = None
                    self.interrupt()
                    self.mode = "listening"
                    speech = []
                    heard_voice = False
                    if self._carry:
                        # "Jarvis … what time is it": the request started while the name was being recognized
                        speech = ([self._carry_audio] if self._carry_audio is not None else []) + (after_spot or [])
                        heard_voice = self._carry_audio is not None or any(
                            float(np.sqrt(np.mean(f ** 2))) > max(0.006, noise * 3.0) for f in speech)
                    self._carry, self._carry_audio, after_spot = False, None, None
                    silence_ms = 0
                    for f in reversed(speech[1:] if heard_voice else []):   # they may have finished already
                        if float(np.sqrt(np.mean(f ** 2))) > max(0.006, noise * 3.0):
                            break
                        silence_ms += 80 if len(f) == FRAME else 0
                    listen_started = now
                    orb_state("listening")
                    bus.emit("listening", on=True)
                continue

            # ---- listening: collect until end of speech
            if self.mode == "listening":
                speech.append(frame)
                threshold = max(0.006, noise * 3.0)
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
            log.debug("wake check heard %r -> %s", text, "woke" if rest is not None else "no")
            if rest is None or self.mode != "idle":
                return
            # "Jarvis, open Spotify": the request came with the name - keep listening until they've actually finished
            # (a pause mid-sentence ended this phrase) and take the whole thing. Just "Jarvis": listen for the request.
            # Either way what was said while this was being checked is kept.
            self._carry_audio = audio if rest else None
            self._carry = True
            self._ptt.set()
        except Exception as e:
            log.warning("wake phrase check failed: %s", e)
        finally:
            self._spotting = False

    async def _cancel_agent(self) -> None:
        from ..agent.engine import agent

        agent.cancel()

    def _process(self, audio: np.ndarray) -> None:
        t0 = time.time()
        try:
            text = self.stt.transcribe(audio)
            if self._wake_phrase:
                rest = match_wake(text, self._wake_phrase)
                text = text if rest is None else rest
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
