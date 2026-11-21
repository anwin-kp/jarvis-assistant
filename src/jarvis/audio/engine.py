"""AudioEngine: the ONLY owner of the microphone.

One continuous input stream is read by one thread; every 30 ms frame goes through `process()`, which turns
audio into events. Nothing else opens the mic, so there are no hand-offs, gaps, double captures or races.

Events (delivered to `on_event(kind, data)` from the engine thread):
    ("level", float)                  input level 0..1, ~10x per second (HUD)
    ("speech_start", None)            someone started talking (HUD: listening)
    ("utterance", Utterance)          a complete thing you said
    ("noise", bool)                   a capture was too short to be speech (True if it was an interrupt)
    ("barge", None)                   you talked over JARVIS: stop speaking now
    ("wake", None)                    wake word heard
    ("capture_timeout", None)         a requested capture (push-to-talk / after wake) heard nothing

Modes:
    "always"  capture whenever someone speaks
    "wake"    wait for the wake word, then capture one utterance
    "push"    capture only after request_capture()
While JARVIS speaks (set_speaking(True), plus a short echo tail afterwards), speech is accepted only as a
barge-in: clearly louder than the learned echo level and not sounding like JARVIS's own voice. The rest of
that utterance is captured on the same stream, so the start of your sentence is never lost.
"""

import collections
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from loguru import logger

RATE = 16000
FRAME = 480  # 30 ms
FRAME_MS = 30


PARTIAL_EVERY = 33  # frames (~1 s) between live caption drafts


@dataclass
class Utterance:
    audio: np.ndarray  # float32, 16 kHz mono
    barge: bool = False  # started while JARVIS was speaking
    after_wake: bool = False  # captured right after the wake word
    voiced_ms: int = 0  # how much actual speech it contains
    short: bool = False  # under min_speech_ms: only accepted if it turns out to be a short command ("stop", "go")


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))


class AudioEngine:
    def __init__(self, on_event: Callable[[str, object], None], *, silence_ms: int = 750, min_speech_ms: int = 250,
                 max_seconds: float = 15, calibrate_ms: int = 1000, min_threshold: float = 0.003,
                 max_threshold: float = 0.03, barge_floor: float = 0.02, barge_factor: float = 1.8,
                 barge_ms: int = 150, echo_tail_ms: int = 700, vad_mode: int = 2,
                 is_speech: Callable[[np.ndarray], bool] | None = None,
                 sounds_like_jarvis: Callable[[np.ndarray], bool] | None = None,
                 wake_detector: Callable[[np.ndarray], bool] | None = None):
        self.emit = on_event
        self.mode = "always"
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        # One-word commands ("stop", "go", "next") are often only 150-220 ms of clear speech: 23% of real
        # captures were thrown away as too short. Down to 120 ms they are kept, marked short, and the app only
        # accepts them if the words are a known short command, so clicks and coughs still do nothing.
        self.min_short_frames = max(1, 120 // FRAME_MS)
        self.max_frames = int(max_seconds * 1000 / FRAME_MS)
        self.calibrate_frames = calibrate_ms // FRAME_MS
        self.min_threshold, self.max_threshold = min_threshold, max_threshold
        self.barge_floor, self.barge_factor = barge_floor, barge_factor
        self.barge_frames = max(1, barge_ms // FRAME_MS)
        self.echo_tail_frames = echo_tail_ms // FRAME_MS
        self.sounds_like_jarvis = sounds_like_jarvis or (lambda audio: False)
        self.wake_detector = wake_detector
        if is_speech is None:
            import webrtcvad

            vad = webrtcvad.Vad(vad_mode)
            is_speech = lambda f: vad.is_speech(f.tobytes(), RATE)  # noqa: E731
        self.is_speech = is_speech

        self.threshold = min_threshold  # speech threshold, follows the room's background level
        self._calib: list[float] = []
        self._idle: collections.deque = collections.deque(maxlen=333)  # ~10 s of quiet-room levels
        self._ring: collections.deque = collections.deque(maxlen=50)  # last 1.5 s
        self._frames: list[np.ndarray] | None = None  # current capture
        self._silence = self._voiced = self._start_run = self._barge_run = 0
        self._capture_barge = self._capture_after_wake = False
        self._speaking = False
        self._tail = 0  # frames of echo tail left after JARVIS stops speaking
        self._echo = 0.0  # learned echo level while JARVIS speaks
        self._echo_learn = 0  # frames left in the echo-learning window at the start of speech
        self._echo_levels: list[float] = []  # mic levels while JARVIS speaks (diagnostics)
        self._voiced_hist: collections.deque = collections.deque(maxlen=10)  # last 300 ms: voiced or not
        self._requested = 0  # frames left for a requested one-shot capture (push-to-talk / after wake)
        self._request_after_wake = False
        self._n = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ control (any thread)
    def set_mode(self, mode: str) -> None:
        assert mode in ("always", "wake", "push")
        with self._lock:
            self.mode = mode

    def set_speaking(self, speaking: bool) -> None:
        with self._lock:
            if speaking and not self._speaking and self._tail == 0:  # a new reply (not just its next sentence)
                self._echo, self._echo_learn = 0.0, 12
                self._voiced_hist.clear()
            if not speaking and self._speaking:
                self._tail = self.echo_tail_frames
                if self._echo_levels:  # measurements for tuning the interrupt thresholds
                    lv = np.array(self._echo_levels)
                    logger.debug("Echo while speaking: p50 {:.4f} p95 {:.4f} max {:.4f}",
                                 float(np.median(lv)), float(np.percentile(lv, 95)), float(lv.max()))
                self._echo_levels = []
            self._speaking = speaking

    def request_capture(self, timeout_s: float = 6, after_wake: bool = False) -> None:
        """Capture the next thing said within timeout_s, in any mode (push-to-talk, follow-up after wake)."""
        with self._lock:
            self._requested = int(timeout_s * 1000 / FRAME_MS)
            self._request_after_wake = after_wake

    @property
    def calibrated(self) -> bool:
        return len(self._calib) >= self.calibrate_frames

    # ------------------------------------------------------------------ frame processing (engine thread)
    def process(self, frame: np.ndarray) -> None:
        frame = np.asarray(frame, dtype=np.int16).reshape(-1)
        level = rms(frame)
        self._n += 1
        if self._n % 3 == 0:
            self.emit("level", min(1.0, level * 8))
        with self._lock:
            speaking = self._speaking or self._tail > 0
            if self._tail > 0:
                self._tail -= 1
            mode = self.mode
        if not self.calibrated:  # learn the room's background level first (never while JARVIS talks)
            if not speaking:
                self._calib.append(level)
                if self.calibrated:
                    self._set_threshold(self._calib)
                    logger.info("Audio engine: speech threshold {:.4f}", self.threshold)
            return
        if not speaking and self._frames is None:
            self._idle.append(level)
            if self._n % 33 == 0 and len(self._idle) >= 33:  # ~1 s: follow the room (fan on/off, ...)
                self._set_threshold(self._idle)
        if self._frames is not None:
            if speaking:
                logger.debug("Capture cut short: JARVIS started speaking")
                self._finish_capture()  # JARVIS started talking: never swallow its voice into your sentence
            else:
                self._continue_capture(frame, level)
                return
        self._ring.append(frame)
        if speaking:
            self._watch_for_barge(frame, level)
            return
        if self._requested > 0:
            self._requested -= 1
            if self._requested == 0:
                self.emit("capture_timeout", None)
                return
        elif mode == "push":
            return
        elif mode == "wake":
            if self.wake_detector and self.wake_detector(frame):
                self.emit("wake", None)
                self.request_capture(6, after_wake=True)
            return
        voiced = level >= self.threshold and self.is_speech(frame)
        self._start_run = self._start_run + 1 if voiced else 0
        if self._start_run >= 2:  # 60 ms of speech: start capturing, with ~600 ms from before it: quiet first
            # sounds ("s", "p", "th") stay under the threshold for a while and were cut ("Stop" -> "Go")
            self._begin_capture(list(self._ring)[-20:], barge=False, after_wake=self._request_after_wake)

    def _set_threshold(self, levels) -> None:
        # median of mostly-quiet frames = the room's background; speech must be clearly above it
        bg = float(np.median(np.asarray(levels)))
        self.threshold = float(min(self.max_threshold, max(self.min_threshold, bg * 3)))

    def _watch_for_barge(self, frame: np.ndarray, level: float) -> None:
        self._echo_levels.append(level)
        if self._echo_learn > 0:  # start of a reply: measure the echo (detection keeps running)
            self._echo = max(self._echo, level)
            self._echo_learn -= 1
        threshold = max(self.barge_floor, self._echo * self.barge_factor)
        voiced = level >= threshold and self.is_speech(frame)
        self._voiced_hist.append(voiced)
        if not voiced:
            if self._echo_learn == 0:
                self._echo = 0.95 * self._echo + 0.05 * level  # keep adapting to the echo level
            return
        # enough speech in the last 300 ms (not necessarily unbroken): short words like "stop" count
        self._barge_run = sum(self._voiced_hist)
        if self._barge_run < self.barge_frames:
            return
        # Clearly louder than the echo (e.g. you speaking into a headset mic): accept on loudness alone.
        # Only borderline cases need the slower voice-fingerprint check.
        strong = level >= max(2 * self.barge_floor, 4 * self._echo)
        recent = np.concatenate(list(self._ring)[-34:]).astype(np.float32) / 32768.0  # ~1 s
        if not strong and self.sounds_like_jarvis(recent):
            self._voiced_hist.clear()
            logger.debug("Barge-in candidate rejected as JARVIS's voice (level {:.4f}, threshold {:.4f})",
                         level, threshold)
            return
        logger.info("Barge-in accepted (level {:.4f}, threshold {:.4f}, echo {:.4f})", level, threshold, self._echo)
        self.emit("barge", None)
        with self._lock:
            self._speaking, self._tail = False, 0
        self._voiced_hist.clear()
        n = 20  # the speech that triggered the interrupt + ~300 ms before it
        self._begin_capture(list(self._ring)[-n:], barge=True, after_wake=False)

    def _begin_capture(self, frames: list, barge: bool, after_wake: bool) -> None:
        self._frames = list(frames)
        self._voiced = self._start_run if not barge else self._barge_run
        self._silence, self._start_run, self._barge_run = 0, 0, 0
        self._capture_barge, self._capture_after_wake = barge, after_wake
        with self._lock:
            self._requested, self._request_after_wake = 0, False
        self.emit("speech_start", None)

    def _continue_capture(self, frame: np.ndarray, level: float) -> None:
        self._frames.append(frame)
        # live captions: about once a second while you talk, the audio so far (the app drafts a caption locally)
        if len(self._frames) % PARTIAL_EVERY == 0 and self._voiced >= 10 and not self._capture_barge:
            self.emit("partial", np.concatenate(self._frames).astype(np.float32) / 32768.0)
        voiced = level >= self.threshold and self.is_speech(frame)
        if voiced:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence < self.silence_frames and len(self._frames) < self.max_frames:
            return
        self._finish_capture()

    def _finish_capture(self) -> None:
        frames, voiced_frames = self._frames, self._voiced
        self._frames = None
        self._ring.clear()
        logger.debug("Capture ended: {:.1f}s, {} voiced frames (need {}), barge={}", len(frames) * FRAME_MS / 1000,
                     voiced_frames, self.min_speech_frames, self._capture_barge)
        if voiced_frames < self.min_short_frames or (voiced_frames < self.min_speech_frames and self._capture_barge):
            self.emit("noise", self._capture_barge)  # True: a very short interrupt ("stop")
            return
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        self.emit("utterance", Utterance(audio, self._capture_barge, self._capture_after_wake,
                                         voiced_frames * FRAME_MS, short=voiced_frames < self.min_speech_frames))

    # ------------------------------------------------------------------ microphone thread
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="audio-engine")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import sounddevice as sd

        while not self._stop.is_set():
            try:
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
                    while not self._stop.is_set():
                        frame, _ = stream.read(FRAME)
                        try:
                            self.process(frame)
                        except Exception:
                            logger.exception("Audio engine frame error")
            except Exception:
                logger.exception("Microphone stream failed; retrying in 2 s")
                self._stop.wait(2)
