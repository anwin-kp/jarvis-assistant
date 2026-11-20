"""Voice ID: JARVIS only obeys the enrolled voice (yours). Offline speaker embeddings via sherpa-onnx.

Enroll once:   .\\jarvis.ps1 enroll-voice     (you read 3 short sentences)
Then set voice.voice_id.enabled: true in config.yaml."""

import sys
from pathlib import Path

import numpy as np
import requests
from loguru import logger

MODEL = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL}"
PHRASES = [
    "Hey Jarvis, what's the weather like today?",
    "Open my documents folder and play some music.",
    "Remind me to call my family at seven in the evening.",
]


class VoiceID:
    def __init__(self, folder: Path, threshold: float = 0.45):
        import sherpa_onnx

        folder.mkdir(parents=True, exist_ok=True)
        model = folder / MODEL
        if not model.exists():
            logger.info("Downloading voice ID model")
            r = requests.get(URL, timeout=120)
            r.raise_for_status()
            model.write_bytes(r.content)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2, provider="cpu")
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.print_file = folder / "voiceprint.npy"
        self.threshold = threshold
        self.voiceprint = np.load(self.print_file) if self.print_file.exists() else None

    @property
    def enrolled(self) -> bool:
        return self.voiceprint is not None

    def embed(self, audio: np.ndarray) -> np.ndarray:
        s = self.extractor.create_stream()
        s.accept_waveform(sample_rate=16000, waveform=np.asarray(audio, dtype=np.float32))
        s.input_finished()
        e = np.asarray(self.extractor.compute(s), dtype=np.float32)
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
    print("Turn it on with voice.voice_id.enabled: true in config/config.yaml, then restart JARVIS.")


if __name__ == "__main__":
    enroll_cli()
