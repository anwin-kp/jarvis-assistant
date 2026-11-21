"""Natural offline text-to-speech with Piper (free voices from rhasspy/piper-voices)."""

import threading
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from loguru import logger

from jarvis.tts.sapi_tts import speakable

VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def voice_url(name: str, suffix: str) -> str:
    """'en_GB-alan-medium' -> .../en/en_GB/alan/medium/en_GB-alan-medium.onnx"""
    locale, speaker, quality = name.split("-")
    return f"{VOICES_URL}/{locale.split('_')[0]}/{locale}/{speaker}/{quality}/{name}{suffix}"


def ensure_voice(name: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    model = folder / f"{name}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        path = folder / f"{name}{suffix}"
        if not path.exists():
            logger.info("Downloading Piper voice {}", path.name)
            r = requests.get(voice_url(name, suffix), timeout=120)
            r.raise_for_status()
            path.write_bytes(r.content)
    return model


class PiperTTS:
    def __init__(self, voice: str, folder: Path, speed: float = 1.0):
        from piper import PiperVoice, SynthesisConfig

        self.voice = PiperVoice.load(str(ensure_voice(voice, folder)))
        self.syn = SynthesisConfig(length_scale=1.0 / max(0.5, min(speed, 2.0)))
        self._stop = threading.Event()

    def speak(self, text: str, should_stop=None) -> None:
        """should_stop(): checked while generating and between playback blocks, so an interrupt is never lost
        (a flag cleared at the start of playback could erase an interrupt that arrived during generation)."""
        self._stop.clear()
        stopped = lambda: self._stop.is_set() or bool(should_stop and should_stop())  # noqa: E731
        chunks = []
        for c in self.voice.synthesize(speakable(text), syn_config=self.syn):
            if stopped():
                return
            chunks.append(c)
        if not chunks or stopped():
            return
        audio = np.concatenate([c.audio_int16_array for c in chunks])
        if self.on_audio is not None:  # what's about to be played (echo recording, audio/aec.py)
            import time

            self.on_audio(audio, chunks[0].sample_rate, time.monotonic())
        # Own output stream, written in small blocks (the global sd.play/sd.stop pair crashed when stopped
        # from another thread while playing).
        block = 1024
        with sd.OutputStream(samplerate=chunks[0].sample_rate, channels=1, dtype="int16") as out:
            for i in range(0, len(audio), block):
                if stopped():
                    break
                out.write(audio[i:i + block].reshape(-1, 1))

    on_audio = None  # (int16 audio, sample rate, start time) -> None

    def stop(self) -> None:
        self._stop.set()
