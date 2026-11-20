from jarvis.core.gaming import GamingMode
from jarvis.tts.piper_tts import voice_url


class FakeBrain:
    def __init__(self):
        self.cpu_only, self.unloaded = False, 0

    def set_cpu_only(self, v):
        self.cpu_only = v

    def unload(self):
        self.unloaded += 1


def test_gaming_mode_toggles_cpu_and_unloads():
    brain, changes = FakeBrain(), []
    g = GamingMode(brain, ["Game.exe"], on_change=lambda a, r: changes.append(a))
    g.set(True, "test")
    assert brain.cpu_only and brain.unloaded == 1 and changes == [True]
    g.set(True, "again")  # no duplicate work
    assert brain.unloaded == 1
    g.set(False)
    assert not brain.cpu_only and changes == [True, False]
    assert g.games == {"game.exe"}


def test_piper_voice_url():
    assert voice_url("en_GB-alan-medium", ".onnx") == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx"
    )
