"""Echo cancellation, measured on synthetic speech-like audio (real recordings: python -m jarvis.audio.aec)."""

import numpy as np

from jarvis.audio.aec import RATE, EchoRecorder, cancel, erle_db, estimate_delay, evaluate

rng = np.random.default_rng(7)


def speechy(seconds: float) -> np.ndarray:
    n = int(seconds * RATE)
    f = np.fft.rfftfreq(n, 1 / RATE)
    shaped = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * (np.exp(-((f - 500) / 400) ** 2)
                                                                   + 0.5 * np.exp(-((f - 1800) / 600) ** 2)), n)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * np.arange(n) / RATE) ** 2
    return (shaped * env / np.abs(shaped).max() * 0.5).astype(np.float32)


REF = speechy(5)
DELAY = int(0.3 * RATE)  # a Bluetooth speaker
IR = np.exp(-np.arange(480) / 120.0) * rng.uniform(0.3, 1, 480)
ECHO = np.convolve(np.concatenate([np.zeros(DELAY), REF]), IR / np.abs(IR).sum() * 3)[: len(REF)].astype(np.float32)


def test_delay_is_found():
    assert abs(estimate_delay(ECHO, REF) - DELAY) <= 16


def test_echo_is_removed():
    res, delay = cancel(ECHO, REF)
    assert erle_db(ECHO, res, ref=REF, delay=delay) > 25


def test_talking_over_it_keeps_your_voice_and_never_makes_the_echo_louder():
    near = np.zeros_like(REF)
    near[3 * RATE:] = speechy(2) * 0.5
    res, _ = cancel(ECHO + near, REF)
    s = slice(3 * RATE + RATE // 4, len(res))
    assert np.corrcoef(res[s], near[s])[0, 1] > 0.95
    left = res[s] - near[s]
    assert np.mean(left ** 2) <= np.mean(ECHO[s] ** 2)  # a single adaptive filter diverged here (+16 dB echo)


def test_recorder_saves_aligned_pairs_and_evaluation_reads_them(tmp_path):
    rec = EchoRecorder(tmp_path)
    t0 = 100.0
    rec.ref((REF * 32767).astype(np.int16), RATE, t0)  # played (int16, like Piper)
    for i in range(0, len(ECHO) // 480 * 480, 480):
        rec.mic(ECHO[i:i + 480], t0 + i / RATE)
    path = rec.flush()
    assert path and path.exists() and path.with_name(path.name.replace("-mic", "-ref")).exists()
    (row,) = evaluate(tmp_path)
    assert abs(row["delay_ms"] - 290) <= 2 and row["erle_db"] > 20  # 300 ms echo, used 10 ms early
    assert rec.flush() is None  # nothing new
