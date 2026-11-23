"""Voice ID: JARVIS only obeys the enrolled voice (yours). Offline speaker embeddings with onnxruntime.

Enroll once:   .\\jarvis.ps1 enroll-voice     (you read 3 short sentences)
voice.voice_id.enabled: auto (default) turns it on once you've enrolled.

The model is 3D-Speaker's CAM++ (VoxCeleb, 512-d embedding). It used to run through sherpa-onnx, whose DLL Smart App
Control blocks on this PC: voice ID never worked, and the same library also powered JARVIS's own-voice fingerprint
(echo rejection when you interrupt), which silently failed too. Now the model runs on onnxruntime (allowed: the
wake word uses it) and the features the model expects - 80 Kaldi log-mel filterbanks per 10 ms frame, minus their
mean over the utterance (the model's metadata: feature_normalize_type global-mean) - are computed in numpy."""

import sys
from pathlib import Path

import numpy as np
import requests
from loguru import logger

MODEL = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL}"
PHRASES = [  # 5 in your normal spot and mic: a voiceprint only matches the setting it was made in
    "Hey Jarvis, what's the weather like today?",
    "Open my documents folder and play some music.",
    "Remind me to call my family at seven in the evening.",
    "Jarvis, what changed in my project today?",
    "Turn the volume down a little and pause the song.",
]
RATE, FRAME, SHIFT, NFFT, MELS = 16000, 400, 160, 512, 80


def _mel(f):
    return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_banks(n_mels: int = MELS, low: float = 20.0, high: float = -400.0) -> np.ndarray:
    """Kaldi's triangular mel filterbank (mel = 1127 ln(1 + f/700)), on the FFT bins."""
    high = RATE / 2 + high if high <= 0 else high
    centers = np.linspace(_mel(low), _mel(high), n_mels + 2)
    bins = _mel(np.arange(NFFT // 2 + 1) * RATE / NFFT)
    banks = np.zeros((n_mels, NFFT // 2 + 1), dtype=np.float32)
    for m in range(n_mels):
        left, center, right = centers[m], centers[m + 1], centers[m + 2]
        banks[m] = np.maximum(0.0, np.minimum((bins - left) / (center - left), (right - bins) / (right - center)))
    return banks


_BANKS = _mel_banks()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / (FRAME - 1))) ** 0.85  # Kaldi's "povey" window


def fbank(audio: np.ndarray) -> np.ndarray:
    """Kaldi-style log-mel filterbank (no dither): (frames, 80). Frames of 25 ms every 10 ms; DC removed and
    pre-emphasis 0.97 per frame; power spectrum."""
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(x) < FRAME:
        x = np.pad(x, (0, FRAME - len(x)))
    n = 1 + (len(x) - FRAME) // SHIFT
    idx = np.arange(FRAME)[None, :] + SHIFT * np.arange(n)[:, None]
    frames = x[idx].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    spec = np.abs(np.fft.rfft(frames * _WINDOW, NFFT)) ** 2
    return np.log(np.maximum(spec @ _BANKS.T, np.finfo(np.float32).eps)).astype(np.float32)


class VoiceID:
    def __init__(self, folder: Path, threshold: float = 0.45):
        import onnxruntime as ort

        folder.mkdir(parents=True, exist_ok=True)
        model = folder / MODEL
        if not model.exists():
            logger.info("Downloading voice ID model")
            r = requests.get(URL, timeout=120)
            r.raise_for_status()
            model.write_bytes(r.content)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads, opts.log_severity_level = 2, 3
        self.session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.print_file = folder / "voiceprint.npy"
        self.threshold = threshold
        self.voiceprint = np.load(self.print_file) if self.print_file.exists() else None
        dim = self.session.get_outputs()[0].shape[-1]
        if self.voiceprint is not None and isinstance(dim, int) and self.voiceprint.shape[-1] != dim:
            logger.warning(r"Saved voiceprint doesn't match the voice model: enroll again (.\jarvis.ps1 enroll-voice)")
            self.voiceprint = None

    @property
    def enrolled(self) -> bool:
        return self.voiceprint is not None

    def embed(self, audio: np.ndarray) -> np.ndarray:
        feats = fbank(audio)
        feats -= feats.mean(axis=0, keepdims=True)  # global-mean normalisation, as the model was trained
        e = self.session.run(None, {self.input: feats[None, :, :]})[0][0].astype(np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def enroll(self, samples: list[np.ndarray]) -> None:
        v = np.mean([self.embed(a) for a in samples], axis=0)
        self.voiceprint = v / (np.linalg.norm(v) + 1e-9)
        np.save(self.print_file, self.voiceprint)

    def score(self, audio: np.ndarray) -> float:
        return float(self.embed(audio) @ self.voiceprint) if self.enrolled else 1.0

    def is_owner(self, audio: np.ndarray) -> tuple[bool, float]:
        if not self.enrolled or audio.size < 16000 * 0.8:  # too short to judge: allow
            return True, 1.0
        s = self.score(audio)
        return s >= self.threshold, s


def enroll_cli() -> None:
    from jarvis.audio.mic import record_until_silence
    from jarvis.core.config import DATA

    vid = VoiceID(DATA / "models" / "voiceid")
    print("Voice enrollment: read each sentence aloud after the prompt (quiet room, normal voice).\n")
    samples = []
    for i, phrase in enumerate(PHRASES, 1):
        input(f"[{i}/{len(PHRASES)}] Press Enter, then say:  \"{phrase}\"")
        audio = record_until_silence(max_seconds=10, start_timeout=8, min_speech_ms=800)
        if audio.size == 0:
            print("  Didn't hear you. Please run enrollment again.")
            sys.exit(1)
        samples.append(audio)
        print(f"  Got it ({audio.size / 16000:.1f}s).")
    vid.enroll(samples)
    print("\nDone. Your voiceprint is saved locally (data/models/voiceid/voiceprint.npy).")
    print("After a restart JARVIS MEASURES how much each utterance sounds like you (nothing is blocked yet).")
    print("In a few days run:  python -m jarvis.audio.voiceid --report   to see if it can tell you apart.")


def report(folder: Path | None = None, db: Path | None = None) -> str:
    """Shadow-mode scores: turns JARVIS answered (mostly you) vs utterances it ignored (TV, other people, its own
    echo). Suggests a threshold only if the two groups really separate."""
    import json
    import sqlite3

    from jarvis.core.config import DATA

    folder, db = folder or DATA / "voice-log", db or DATA / "jarvis.db"
    rows = sqlite3.connect(db).execute("SELECT text FROM messages WHERE role='user'")
    answered = {r[0].strip().lower() for r in rows}
    yours, others = [], []
    for j in folder.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("voice_score") is None or not d.get("text"):
            continue
        (yours if d["text"].strip().lower() in answered else others).append(float(d["voice_score"]))
    if len(yours) < 10 or len(others) < 10:
        return (f"Not enough data yet: {len(yours)} answered and {len(others)} ignored utterances with a score "
                "(need 10 each). Keep using JARVIS for a few days.")
    low_you, high_other = float(np.percentile(yours, 10)), float(np.percentile(others, 90))
    head = (f"Answered (mostly you): {len(yours)}, median {np.median(yours):.2f}, 10th percentile {low_you:.2f}\n"
            f"Ignored (TV, others):  {len(others)}, median {np.median(others):.2f}, 90th percentile {high_other:.2f}\n")
    if low_you - high_other < 0.05:
        return head + ("They overlap: voice ID can't reliably tell you apart in this setup. Enroll again in your "
                       "usual spot (.\\jarvis.ps1 enroll-voice), or keep it off.")
    th = round((low_you + high_other) / 2, 2)
    return head + (f"They separate. Suggested threshold {th}: set voice.voice_id.threshold: {th} and "
                   "voice.voice_id.enabled: true in config.yaml, then restart.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        print(report())
    else:
        enroll_cli()
