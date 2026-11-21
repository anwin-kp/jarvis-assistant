"""Microphone recording with voice-activity detection (stops when you stop talking)."""

import collections
import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd
import webrtcvad

RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000  # 480 samples


def record_until_silence(
    max_seconds: float = 15,
    silence_ms: int = 800,
    start_timeout: float | None = 6,
    aggressiveness: int = 2,
    stop: threading.Event | None = None,
    on_level: Callable[[float], None] | None = None,
    min_speech_ms: int = 0,
    min_rms: float = 0.006,
    on_start: Callable[[], None] | None = None,
    prefix: np.ndarray | None = None,
) -> np.ndarray:
    """Record from the default mic until `silence_ms` of silence after speech.

    start_timeout: give up if nobody speaks within this many seconds (None = wait forever).
    stop: abort (return empty) as soon as this event is set.
    on_level: called with the input level (0..1) about 10x per second, e.g. for the HUD.
    min_speech_ms: discard recordings with less speech than this (coughs, clicks, short noises).
    min_rms: frames quieter than this never count as speech (room hiss, distant sounds).
    Returns float32 mono audio at 16 kHz, or an empty array.
    """
    vad = webrtcvad.Vad(aggressiveness)
    need = silence_ms // FRAME_MS
    recent: collections.deque[bool] = collections.deque(maxlen=need)
    pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=10)  # keep ~300 ms before speech
    frames: list[np.ndarray] = []
    started, speech_frames, waited, n = False, 0, 0, 0
    if prefix is not None and prefix.size:  # speech already captured (e.g. you interrupted JARVIS)
        frames.append((np.clip(prefix, -1, 1) * 32767).astype(np.int16).reshape(-1, 1))
        started, speech_frames = True, int(prefix.size / FRAME)
    max_frames = int(max_seconds * 1000 / FRAME_MS)
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        while True:
            if stop is not None and stop.is_set():
                return np.zeros(0, dtype=np.float32)
            block, _ = stream.read(FRAME)
            n += 1
            rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
            if on_level and n % 3 == 0:
                on_level(min(1.0, rms * 8))
            speech = rms >= min_rms and vad.is_speech(block.tobytes(), RATE)
            if not started:
                pre_roll.append(block.copy())
                if speech:
                    started = True
                    frames.extend(pre_roll)
                    speech_frames = 1
                    if on_start:
                        on_start()
                else:
                    waited += 1
                    if start_timeout is not None and waited * FRAME_MS > start_timeout * 1000:
                        break
                continue
            frames.append(block.copy())
            recent.append(speech)
            speech_frames += speech
            if (len(recent) == need and not any(recent)) or len(frames) >= max_frames:
                break
    if not frames or speech_frames * FRAME_MS < min_speech_ms:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).flatten().astype(np.float32) / 32768.0


def calibrate_noise(seconds: float = 1.0, floor: float = 0.003, ceiling: float = 0.03) -> float:
    """Measure the room's background level and return a speech threshold just above it."""
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        levels = []
        for _ in range(int(seconds * 1000 / FRAME_MS)):
            block, _ = stream.read(FRAME)
            levels.append(float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2))))
    return float(min(ceiling, max(floor, np.percentile(levels, 80) * 2.5)))
