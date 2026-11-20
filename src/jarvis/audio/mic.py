"""Microphone recording with voice-activity detection (stops when you stop talking)."""

import collections

import numpy as np
import sounddevice as sd
import webrtcvad

RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000  # 480 samples


def record_until_silence(
    max_seconds: float = 15, silence_ms: int = 800, start_timeout: float = 6, aggressiveness: int = 2
) -> np.ndarray:
    """Record from the default mic until `silence_ms` of silence after speech.
    Returns float32 mono audio at 16 kHz (empty if nobody spoke within `start_timeout`)."""
    vad = webrtcvad.Vad(aggressiveness)
    need = silence_ms // FRAME_MS
    recent: collections.deque[bool] = collections.deque(maxlen=need)
    pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=10)  # keep ~300 ms before speech
    frames: list[np.ndarray] = []
    started = False
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        for i in range(int(max_seconds * 1000 / FRAME_MS)):
            block, _ = stream.read(FRAME)
            speech = vad.is_speech(block.tobytes(), RATE)
            if not started:
                pre_roll.append(block.copy())
                if speech:
                    started = True
                    frames.extend(pre_roll)
                elif i * FRAME_MS > start_timeout * 1000:
                    break
                continue
            frames.append(block.copy())
            recent.append(speech)
            if len(recent) == need and not any(recent):
                break
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).flatten().astype(np.float32) / 32768.0
