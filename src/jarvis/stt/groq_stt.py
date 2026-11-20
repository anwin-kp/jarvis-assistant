"""Cloud speech recognition: Whisper large-v3-turbo on Groq (free tier, ~0.3-0.6 s, much more accurate
than the local small model, especially for accents). Audio is sent to Groq; local Whisper is the fallback."""

import io
import wave

import numpy as np
import requests

from jarvis.stt.whisper_stt import is_hallucination

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
LANG_CODES = {"english": "en", "hindi": "hi", "malayalam": "ml", "tamil": "ta", "telugu": "te", "kannada": "kn",
              "marathi": "mr", "bengali": "bn", "gujarati": "gu", "punjabi": "pa", "urdu": "ur", "spanish": "es",
              "french": "fr", "german": "de", "arabic": "ar", "chinese": "zh", "japanese": "ja"}


def to_wav(audio: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class GroqSTT:
    device = "groq"

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo", vocabulary: str | None = None,
                 languages: list[str] | None = None, timeout: float = 2.5):
        self.key, self.model, self.vocabulary, self.timeout = api_key, model, vocabulary, timeout
        self.languages = [lang.lower() for lang in (languages or [])]

    def transcribe(self, audio, language: str | None = None) -> tuple[str, str]:
        if audio.size < 16000 * 0.3:
            return "", ""
        lang = language or (self.languages[0] if len(self.languages) == 1 else None)
        data = {"model": self.model, "response_format": "verbose_json", "temperature": "0"}
        if lang:
            data["language"] = lang
        if self.vocabulary:
            data["prompt"] = self.vocabulary
        r = requests.post(URL, headers={"Authorization": f"Bearer {self.key}"}, data=data,
                          files={"file": ("speech.wav", to_wav(audio), "audio/wav")}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        segs = j.get("segments") or []
        if segs:
            text = " ".join(s["text"].strip() for s in segs
                            if s.get("no_speech_prob", 0) < 0.6 and s.get("avg_logprob", 0) > -1.0).strip()
        else:
            text = (j.get("text") or "").strip()
        detected = LANG_CODES.get(str(j.get("language", "")).lower(), lang or "en")
        return ("" if is_hallucination(text) else text), detected
