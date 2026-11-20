import numpy as np

from jarvis.core.fast_intents import FastIntents, normalize
from jarvis.llm.semantic import SemanticGroups, needs_thinking
from jarvis.tools.registry import ToolRegistry

OK = lambda t, a: True


def make_reg():
    reg, calls = ToolRegistry(), []

    def tool(name, props, group="system", result="ok"):
        @reg.tool(name, f"{name.replace('_', ' ')} tool", props, group=group)
        def f(**kw):
            calls.append((name, kw))
            return result

    tool("media_control", {"action": {"type": "string"}})
    tool("set_volume", {"percent": {"type": "integer"}})
    tool("get_volume", {}, result='{"percent": 50, "muted": false}')
    tool("set_mute", {"muted": {"type": "boolean"}})
    tool("lock_screen", {})
    tool("open_app", {"name": {"type": "string"}})
    tool("set_dark_mode", {"on": {"type": "boolean"}}, group="pc")
    tool("cancel_shutdown", {}, result="Cancelled.")
    return reg, calls


def test_normalize():
    assert normalize("Hey Jarvis, please pause the music now!") == "pause the music"


def test_fast_intents():
    reg, calls = make_reg()
    fast = FastIntents(reg, {"security": {"app_allowlist": {"notepad": "notepad.exe"}}})
    assert fast.try_handle("Jarvis, pause the music", OK) == "Paused."
    assert fast.try_handle("skip this song", OK) == "Skipping."
    assert fast.try_handle("set the volume to 35%", OK) == "Volume 35 percent."
    assert fast.try_handle("turn the volume up", OK) == "Volume 60 percent."
    assert fast.try_handle("mute", OK) == "Muted."
    assert fast.try_handle("lock my computer", OK) == "Locking."
    assert fast.try_handle("open notepad", OK) == "Opening notepad."
    assert fast.try_handle("turn on dark mode", OK) == "Dark mode on."
    assert fast.try_handle("what time is it?", OK).startswith("It's")
    assert fast.try_handle("never mind, don't shut down", OK) == "Cancelled."
    assert ("media_control", {"action": "next"}) in calls
    # not instant: unknown apps, real questions, anything longer
    for t in ["open my resume", "pause and tell me a joke", "what's the weather", "play Believer by Imagine Dragons"]:
        assert fast.try_handle(t, OK) is None, t


def test_needs_thinking():
    assert needs_thinking("why does my laptop overheat when gaming?")
    assert needs_thinking("compare the pixel 9 and iphone 16")
    assert not needs_thinking("what's the weather in Kochi")
    assert not needs_thinking("write a birthday message for mom")


def test_semantic_groups_pick_by_meaning():
    reg = ToolRegistry()
    for name, group in [("set_brightness", "system"), ("read_screen", "screen"), ("play_youtube", "media")]:
        reg.tool(name, f"{name} description", {}, group=group)(lambda: "ok")
    vocab = {"bright": 0, "screen": 1, "dim": 0, "music": 2, "song": 2, "youtube": 2, "read": 3, "text": 3}

    def embed(texts):  # toy "meaning" vectors
        out = []
        for t in texts:
            v = np.full(4, 0.05)
            for w, i in vocab.items():
                if w in t.lower():
                    v[i] += 1
            out.append(v)
        return np.array(out)

    sem = SemanticGroups(reg, embed, top_tools=1, min_score=0.5)
    assert sem.pick("make it less bright, too much brightness") == {"system"}
    assert sem.pick("put on a song") == {"media"}


def test_fast_intents_matches_without_side_effects():
    reg, calls = make_reg()
    fast = FastIntents(reg, {})
    assert fast.matches("next song") and fast.matches("Jarvis, pause")
    assert not fast.matches("He's moved") and not fast.matches("tell me about Kerala")
    assert calls == []
