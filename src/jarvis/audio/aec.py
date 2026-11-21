"""Acoustic echo cancellation (AEC): remove JARVIS's own voice from the microphone.

Why: today JARVIS *estimates* how loud its echo is and only accepts an interruption that's clearly louder. With a
loud Bluetooth speaker the echo can be louder than your voice, so you have to shout. JARVIS knows exactly what it
is playing (the reference), so the echo can be predicted and subtracted instead:

    mic = your voice + room(reference delayed by the speaker)      ->   residual = mic - estimated echo

1. Bulk delay: Bluetooth adds ~0.2-0.5 s. GCC-PHAT cross-correlation finds it.
2. Room echo: a partitioned-block frequency-domain adaptive filter (PBFDAF, NLMS per frequency bin) learns the
   room's response (P x M taps; 8 x 256 = 128 ms at 16 kHz) and keeps adapting.

Status: measured offline first. `voice.record_echo: true` saves (mic, reference) pairs while JARVIS speaks
(data/voice-log/echo, local only); `python -m jarvis.audio.aec` reports per recording how much echo would be removed
(ERLE, dB). It is wired into interrupting only once real recordings show it works.
"""

import time
import wave
from pathlib import Path

import numpy as np

RATE = 16000
SAFETY = 160  # samples (10 ms) the bulk delay is shortened by; the adaptive filter covers the rest


def estimate_delay(mic: np.ndarray, ref: np.ndarray, max_delay_s: float = 0.8) -> int:
    """Samples by which the echo of ref lags in mic (GCC-PHAT: robust to the room's colouring)."""
    n = len(mic) + len(ref)
    nfft = 1 << (n - 1).bit_length()
    R = np.fft.rfft(ref, nfft)
    cross = np.fft.rfft(mic, nfft) * np.conj(R)
    # whiten only where the reference has energy: speech leaves many bins near zero, and whitening those (pure
    # numerical noise) made a false peak at 0 ms beat the real 300 ms echo in tests
    used = np.abs(R) > 1e-3 * np.abs(R).max()
    cc = np.fft.irfft(np.where(used, cross / (np.abs(cross) + 1e-12), 0), nfft)
    lag_max = min(int(max_delay_s * RATE), len(mic) - 1)
    return int(np.argmax(np.abs(cc[: lag_max + 1])))


class EchoCanceller:
    """Partitioned-block frequency-domain NLMS. Feed blocks of M samples: mic and the (already delay-aligned)
    reference; get the mic with the echo removed."""

    def __init__(self, block: int = 256, partitions: int = 8, mu: float = 0.5, delay: int = 0):
        self.M, self.P, self.mu = block, partitions, mu
        self.W = np.zeros((partitions, block + 1), complex)  # filter, per partition, per frequency bin
        self.X = np.zeros((partitions, block + 1), complex)  # recent reference spectra (newest first)
        self.prev_ref = np.zeros(block)
        self.power = np.full(block + 1, 1e-6)
        self.delay = delay
        self._ref_buf = np.zeros(delay)  # reference held back by the bulk delay

    W_fg = None  # foreground filter: the one whose output is used

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """mic, ref: float32 blocks of `block` samples (ref = what was played at the same time).

        Two filters (measured on synthetic speech: a single one diverged as soon as you talked over it, making the
        echo 16 dB *louder*): the background filter always adapts; the foreground one, whose output is used, only
        copies it when the background clearly removes more. Your voice can't be 'learned away'."""
        M = self.M
        stream = np.concatenate([self._ref_buf, ref])
        ref, self._ref_buf = stream[:M], stream[M:]
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = np.fft.rfft(np.concatenate([self.prev_ref, ref]))
        self.prev_ref = ref
        if self.W_fg is None:
            self.W_fg = self.W.copy()
        err = mic - np.fft.irfft((self.W * self.X).sum(0))[M:]  # background
        out = mic - np.fft.irfft((self.W_fg * self.X).sum(0))[M:]  # foreground
        e_bg, e_fg, e_mic = float(err @ err), float(out @ out), float(mic @ mic)
        if e_bg < 0.8 * e_fg and e_bg < e_mic:
            self.W_fg, out = self.W.copy(), err
        elif e_bg > 4 * e_mic + 1e-9 and e_fg <= e_mic:  # the background ran away (double talk): restart it
            self.W = self.W_fg.copy()
        E = np.fft.rfft(np.concatenate([np.zeros(M), err]))
        self.power = 0.9 * self.power + 0.1 * (np.abs(self.X[0]) ** 2)
        # regularised per-bin step: speech leaves most bins nearly empty, and dividing by ~0 let mic noise in
        delta = 0.05 * float(self.power.mean()) + 1e-8
        grad = np.conj(self.X) * (E / (self.power * self.P + delta))
        g = np.fft.irfft(grad, axis=1)
        g[:, M:] = 0  # constrain each partition to M taps (overlap-save)
        self.W += self.mu * np.fft.rfft(g, axis=1)
        return out


def cancel(mic: np.ndarray, ref: np.ndarray, delay: int | None = None, block: int = 256) -> tuple[np.ndarray, int]:
    """Whole recordings: (residual, delay used)."""
    # 10 ms early: the estimate can land a few samples late, and a reference delayed past the echo's start can
    # never model its strongest part (tests: 1 dB removed with the raw estimate, 36 dB with this margin)
    delay = max(0, estimate_delay(mic, ref) - SAFETY) if delay is None else delay
    aec = EchoCanceller(block=block, delay=delay)
    n = len(mic) // block * block
    ref = np.pad(ref, (0, max(0, n - len(ref))))[:n]
    out = np.concatenate([aec.process(mic[i:i + block], ref[i:i + block]) for i in range(0, n, block)])
    return out, delay


def erle_db(mic: np.ndarray, residual: np.ndarray, skip_s: float = 1.0, ref: np.ndarray | None = None,
            delay: int = 0, block: int = 512) -> float:
    """Echo return loss enhancement: how much quieter the echo got (after the filter's first second).
    With ref: only the blocks where the echo dominates (the loudest third of the delayed reference). Room noise
    can't be cancelled, so counting quiet blocks measured the noise floor, not the canceller (seen in tests)."""
    s = int(skip_s * RATE)
    m, r = mic[s:], residual[s:len(mic)]
    if ref is not None:
        aligned = np.concatenate([np.zeros(delay), ref])[s:s + len(m)]
        n = min(len(m), len(r), len(aligned)) // block * block
        energy = (aligned[:n].reshape(-1, block) ** 2).mean(1)
        loud = energy >= np.quantile(energy, 2 / 3) if len(energy) else energy
        m, r = m[:n].reshape(-1, block)[loud], r[:n].reshape(-1, block)[loud]
    return float(10 * np.log10((np.mean(m ** 2) + 1e-12) / (np.mean(r ** 2) + 1e-12)))


# ------------------------------------------------------------------ evidence: record (mic, reference) pairs


def _write(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


class EchoRecorder:
    """While JARVIS speaks: the mic frames and what was played, on one clock, saved as two WAVs per reply."""

    def __init__(self, folder: Path, keep: int = 20):
        self.folder, self.keep = folder, keep
        self.mic_frames: list[tuple[float, np.ndarray]] = []
        self.played: list[tuple[float, np.ndarray]] = []

    def mic(self, frame: np.ndarray, t: float | None = None) -> None:
        self.mic_frames.append((t if t is not None else time.monotonic(), np.asarray(frame, np.float32)))

    def ref(self, audio: np.ndarray, rate: int, t: float | None = None) -> None:
        """audio as played (any rate), t = when playback started."""
        a = np.asarray(audio)
        a = a.astype(np.float32) / 32768 if np.issubdtype(a.dtype, np.integer) else a.astype(np.float32)
        if rate != RATE:
            a = np.interp(np.arange(0, len(a), rate / RATE), np.arange(len(a)), a).astype(np.float32)
        self.played.append((t if t is not None else time.monotonic(), a))

    def flush(self) -> Path | None:
        """End of a reply: write <stamp>-mic.wav and <stamp>-ref.wav (aligned on the mic's clock)."""
        mic, played = self.mic_frames, self.played
        self.mic_frames, self.played = [], []
        if len(mic) < 33 or not played:  # under ~1 s: nothing to learn from
            return None
        t0 = mic[0][0]
        mic_audio = np.concatenate([f for _, f in mic])
        ref_audio = np.zeros_like(mic_audio)
        for t, a in played:
            start = max(0, int((t - t0) * RATE))
            end = min(len(ref_audio), start + len(a))
            if end > start:
                ref_audio[start:end] += a[: end - start]
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = self.folder / time.strftime("%Y%m%d-%H%M%S")
        _write(Path(f"{stem}-mic.wav"), mic_audio)
        _write(Path(f"{stem}-ref.wav"), ref_audio)
        for old in sorted(self.folder.glob("*-mic.wav"))[:-self.keep]:
            old.unlink(missing_ok=True)
            Path(str(old).replace("-mic.wav", "-ref.wav")).unlink(missing_ok=True)
        return Path(f"{stem}-mic.wav")


def evaluate(folder: Path) -> list[dict]:
    """Per recorded reply: echo delay, echo level, and how much the canceller would remove."""
    out = []
    for m in sorted(folder.glob("*-mic.wav")):
        mic, ref = _read(m), _read(Path(str(m).replace("-mic.wav", "-ref.wav")))
        residual, delay = cancel(mic, ref)
        out.append({"file": m.name, "seconds": round(len(mic) / RATE, 1), "delay_ms": round(1000 * delay / RATE),
                    "echo_rms": round(float(np.sqrt(np.mean(mic ** 2))), 4),
                    "erle_db": round(erle_db(mic, residual, ref=ref, delay=delay), 1)})
    return out


if __name__ == "__main__":
    from jarvis.core.config import DATA

    rows = evaluate(DATA / "voice-log" / "echo")
    if not rows:
        print("No recordings yet: set voice.record_echo: true, restart, let JARVIS talk a few times, then run this.")
    for r in rows:
        print(f"{r['file']}: {r['seconds']}s, echo delay {r['delay_ms']} ms, echo level {r['echo_rms']}, "
              f"removed {r['erle_db']} dB")
    if rows:
        print(f"Median echo removed: {float(np.median([r['erle_db'] for r in rows])):.1f} dB "
              "(>= 15 dB: worth wiring into interrupting)")
