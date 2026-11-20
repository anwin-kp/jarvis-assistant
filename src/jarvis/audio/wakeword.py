"""'Hey Jarvis' wake-word detection with openWakeWord (offline, CPU)."""

import threading

import numpy as np
import sounddevice as sd


class WakeWord:
    def __init__(self, threshold: float = 0.5, model: str = "hey_jarvis"):
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([model])
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.threshold = threshold

    def wait(self, stop: threading.Event) -> bool:
        """Block until the wake word is heard (True) or `stop` is set (False)."""
        self.model.reset()
        hits = 0
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280) as stream:
            while not stop.is_set():
                frame, _ = stream.read(1280)  # 80 ms
                score = max(self.model.predict(np.asarray(frame).flatten()).values())
                hits = hits + 1 if score >= self.threshold else 0
                if hits >= 2:  # two consecutive frames: fewer false wakes
                    return True
        return False
