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
                 download_root: str | None = None):
        if device == "cuda":
            _add_cuda_dlls()
        from faster_whisper import WhisperModel

        try:
            self.model = WhisperModel(model, device=device, compute_type=compute_type, download_root=download_root)
            self.device = device
            self._warmup()
        except Exception as e:
            logger.warning("Whisper on {} failed ({}); using CPU", device, e)
            self.model = WhisperModel(model, device="cpu", compute_type="int8", download_root=download_root)
            self.device = "cpu"
        logger.info("Whisper {} loaded on {}", model, self.device)

    def _warmup(self) -> None:
        import numpy as np

        # Forces CUDA kernels/DLLs to load now, so failures trigger the CPU fallback.
        list(self.model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])

    def transcribe(self, audio, language: str | None = None) -> tuple[str, str]:
        """audio: float32 16 kHz numpy array or a file path. Returns (text, language)."""
        if hasattr(audio, "size") and audio.size < 16000 * 0.3:
            return "", ""
        segments, info = self.model.transcribe(audio, language=language, beam_size=1, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language
