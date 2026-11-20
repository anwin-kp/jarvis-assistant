from jarvis.core.orchestrator import Orchestrator, explicit_remember_request, strip_filler
from jarvis.memory.store import Memory
from jarvis.tools import memory_tools
from jarvis.tools.registry import ToolRegistry


def test_explicit_remember_detection():
    assert explicit_remember_request("remember that I prefer short answers") == "I prefer short answers"
    assert explicit_remember_request("Jarvis, please remember my car is blue.") == "my car is blue"
    assert explicit_remember_request("do you remember my name?") is None
    assert explicit_remember_request("remember when we talked about Rust?") is None
    assert explicit_remember_request("what's the weather") is None


def test_strip_filler():
    assert strip_filler("Volume set to 40%. Let me know if you need anything else.") == "Volume set to 40%."
    assert strip_filler("Done! Feel free to ask more questions!") == "Done!"
    assert strip_filler("Let me know.") == "Let me know."  # never return an empty reply
    assert strip_filler("It's 29°C in Kochi.") == "It's 29°C in Kochi."


class LazyBrain:
    """Mimics a model that claims to remember without calling the tool."""

    last_tool_calls: list[str] = []

    def respond(self, messages, tools, confirm):
        return "Remembered."


def test_safety_net_saves_fact_when_model_forgets(tmp_path):
    mem = Memory(tmp_path / "m.db")
    reg = ToolRegistry()
    memory_tools.register(reg, mem)
    orch = Orchestrator(LazyBrain(), reg, mem, persona="test")
    orch.handle("remember that I like masala chai", confirm=lambda t, a: True)
    assert [f["fact"] for f in mem.list_facts()] == ["I like masala chai"]
