"""Speech-to-text with faster-whisper (GPU if available, CPU fallback)."""

import os
import sys
from pathlib import Path

from loguru import logger


def _add_cuda_dlls() -> None:
    """Make the pip-installed cuBLAS/cuDNN DLLs visible on Windows."""
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn"):
        d = base / sub / "bin"
        if d.exists():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


class WhisperSTT:
    def __init__(self, model: str = "small", device: str = "cuda", compute_type: str = "float16",
                 download_root: str | None = None, vocabulary: str | None = None,
                 languages: list[str] | None = None):
        self.languages = [lang.lower() for lang in (languages or [])]  # e.g. ['en', 'hi', 'ml']
        self.vocabulary = vocabulary  # names/places to help recognition, e.g. "Jarvis, Kochi"
        if device == "cuda":
            _add_cuda_dlls()
        from faster_whisper import WhisperModel

        self.model = None
        if device == "cuda":
            # some modes (e.g. int8 on new GPU generations) aren't supported: try float16 before the CPU
            for ct in dict.fromkeys([compute_type, "float16"]):
                try:
                    self.model = WhisperModel(model, device="cuda", compute_type=ct, download_root=download_root)
                    self._warmup()
                    self.device, self.compute_type = "cuda", ct
                    break
                except Exception as e:
                    logger.warning("Whisper on cuda/{} failed ({})", ct, e)
                    self.model = None
        if self.model is None:
            logger.warning("Whisper using the CPU (slower)")
            self.model = WhisperModel(model, device="cpu", compute_type="int8", download_root=download_root)
            self.device, self.compute_type = "cpu", "int8"
        logger.info("Whisper {} loaded on {}", model, self.device)

    def _warmup(self) -> None:
        import numpy as np

        # Forces CUDA kernels/DLLs to load now, so failures trigger the CPU fallback.
        list(self.model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])

    def transcribe(self, audio, language: str | None = None) -> tuple[str, str]:
        """audio: float32 16 kHz numpy array or a file path. Returns (text, language)."""
        if hasattr(audio, "size") and audio.size < 16000 * 0.3:
            return "", ""
        segments, info = self.model.transcribe(audio, language=language, beam_size=1, vad_filter=True,
                                                initial_prompt=self.vocabulary or None)
        if not language and self.languages and info.language not in self.languages:
            # short phrases are sometimes mis-detected: retry in the most likely of *your* languages
            probs = dict(info.all_language_probs or [])
            best = max(self.languages, key=lambda lang: probs.get(lang, 0))
            segments, info = self.model.transcribe(audio, language=best, beam_size=1, vad_filter=True,
                                                    initial_prompt=self.vocabulary or None)
        # Drop segments Whisper itself thinks are not speech or is very unsure about (noise, TV murmur).
        kept = [s.text.strip() for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0]
        text = " ".join(kept).strip()
        if is_hallucination(text):
            return "", info.language
        return text, info.language


# Phrases Whisper typically "hears" in silence or background noise.
_HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
    "bye", "bye bye", "okay", "ok", "uh", "um", "hmm", "mm", "oh", "so", "yeah", "please subscribe",
    "subtitles by the amara.org community",
}


def is_hallucination(text: str) -> bool:
    t = "".join(ch for ch in text.lower() if ch.isalnum() or ch in " .").strip(" .")
    return t in _HALLUCINATIONS or len(t) < 2
