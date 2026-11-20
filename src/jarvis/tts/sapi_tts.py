"""Text-to-speech with built-in Windows voices (offline, zero setup)."""

import re

import pyttsx3


def speakable(text: str) -> str:
    """Make model output sound natural when read aloud."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # markdown links -> label
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_#`>|]", "", text)
    text = text.replace("°C", " degrees Celsius").replace("°F", " degrees Fahrenheit").replace("%", " percent")
    return re.sub(r"\s+", " ", text).strip()


class SapiTTS:
    def __init__(self, rate: int = 185, voice: str | None = None):
        self.rate = rate
        self.voice = voice  # part of a voice name, e.g. "David" or "Zira"

    def speak(self, text: str) -> None:
        # A fresh engine per utterance avoids pyttsx3's known hang on repeated runAndWait().
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        if self.voice:
            for v in engine.getProperty("voices"):
                if self.voice.lower() in v.name.lower():
                    engine.setProperty("voice", v.id)
                    break
        engine.say(speakable(text))
        engine.runAndWait()
        engine.stop()

    def save(self, text: str, path: str) -> None:
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.save_to_file(speakable(text), path)
        engine.runAndWait()
        engine.stop()
