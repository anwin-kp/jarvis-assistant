"""Natural offline text-to-speech with Piper (free voices from rhasspy/piper-voices)."""

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

    def speak(self, text: str) -> None:
        chunks = list(self.voice.synthesize(speakable(text), syn_config=self.syn))
        if not chunks:
            return
        audio = np.concatenate([c.audio_int16_array for c in chunks])
        sd.play(audio, samplerate=chunks[0].sample_rate)
        sd.wait()

    def stop(self) -> None:
        sd.stop()
