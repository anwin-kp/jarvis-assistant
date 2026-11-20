"""'Hey Jarvis' wake-word detection with openWakeWord (offline, CPU).
Fed frame by frame by the AudioEngine (it never opens the microphone itself)."""

import numpy as np


class WakeWord:
    CHUNK = 1280  # openWakeWord works on 80 ms chunks

    def __init__(self, threshold: float = 0.5, model: str = "hey_jarvis"):
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([model])
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.threshold = threshold
        self._buf = np.zeros(0, dtype=np.int16)
        self._hits = 0

    def process(self, frame: np.ndarray) -> bool:
        """Feed a 30 ms frame; True once the wake word is heard (two consecutive hits: fewer false wakes)."""
        self._buf = np.concatenate([self._buf, np.asarray(frame, dtype=np.int16).reshape(-1)])
        while self._buf.size >= self.CHUNK:
            chunk, self._buf = self._buf[: self.CHUNK], self._buf[self.CHUNK:]
            score = max(self.model.predict(chunk).values())
            self._hits = self._hits + 1 if score >= self.threshold else 0
            if self._hits >= 2:
                self._hits = 0
                self.model.reset()
                self._buf = np.zeros(0, dtype=np.int16)
                return True
        return False
