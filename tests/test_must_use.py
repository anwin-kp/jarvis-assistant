"""Live-data questions must use their tool: the smoke test heard invented temperatures, tabs and a stale date."""

import threading
import types

from jarvis.core.orchestrator import required_tools
from jarvis.llm.ollama_brain import OllamaBrain
from jarvis.tools.registry import ToolRegistry

ALL = {"pc_temperatures", "list_browser_tabs", "date_math", "battery_health", "network_status", "web_search",
       "now_playing", "get_system_info"}


def test_the_three_smoke_test_questions_get_their_tool():
    assert required_tools("is my laptop overheating", ALL) == {"pc_temperatures"}
    assert required_tools("what tabs do I have open in chrome", ALL) == {"list_browser_tabs"}
    assert required_tools("how many days until christmas", ALL) == {"date_math"}
    assert required_tools("what's the price of bitcoin right now?", ALL) == {"web_search"}
    assert required_tools("tell me a joke", ALL) == set()
    assert required_tools("is my laptop overheating", {"web_search"}) == set()  # not offered: nothing to require


def brain_with(replies, reg):
    b = OllamaBrain.__new__(OllamaBrain)
    b.reg, b.model, b.think, b.keep_alive, b.options, b.max_steps = reg, "m", False, 0, {}, 6
    b.cancel = threading.Event()
    it = iter(replies)
    b.client = types.SimpleNamespace(chat=lambda **kw: types.SimpleNamespace(message=next(it)))
    b._chat_stream = lambda *a, **k: (_ for _ in ()).throw(AssertionError("streamed before the tool ran"))
    return b


def call(name):
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments={}))


def test_an_answer_from_memory_is_pushed_back_and_never_spoken():
    reg, spoken = ToolRegistry(), []

    @reg.tool("pc_temperatures", "temps", {}, read_only=True)
    def pc_temperatures():
        return "GPU 47 C; CPU: not exposed"

    replies = [types.SimpleNamespace(content="CPU 45 C and GPU 41 C: all fine.", tool_calls=None),  # invented
               types.SimpleNamespace(content="", tool_calls=[call("pc_temperatures")])]
    b = brain_with(replies, reg)

    def stream(msgs, tools, think, on_sentence):  # allowed only once the live-data tool has run
        assert b.last_tool_calls == ["pc_temperatures"], "streamed before the tool ran"
        on_sentence("The GPU is at 47 C; the CPU isn't readable.")
        return types.SimpleNamespace(content="The GPU is at 47 C; the CPU isn't readable.", tool_calls=None)

    b._chat_stream = stream
    out = b.respond([], reg.schemas(), lambda t, a: True, on_sentence=spoken.append, must_use={"pc_temperatures"})
    assert out == "The GPU is at 47 C; the CPU isn't readable." and b.last_tool_calls == ["pc_temperatures"]
    assert spoken == ["The GPU is at 47 C; the CPU isn't readable."]  # the invented first answer was never spoken


def test_without_a_requirement_answers_stream_as_before():
    reg, spoken = ToolRegistry(), []
    b = brain_with([], reg)
    b._chat_stream = lambda msgs, tools, think, on_sentence: (on_sentence("Hi."), types.SimpleNamespace(
        content="Hi.", tool_calls=None))[1]
    assert b.respond([], [], None, on_sentence=spoken.append) == "Hi." and spoken == ["Hi."]


def test_cloud_brain_pushes_back_too(monkeypatch):
    import json

    from jarvis.llm import cloud_brain

    reg = ToolRegistry()

    @reg.tool("date_math", "dates", {"target": {"type": "string"}}, read_only=True)
    def date_math(target):
        return "Friday 25 December 2026 is 32 days from 23 November 2026."

    answers = iter([{"content": "There are 34 days until Christmas."},
                    {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": "date_math", "arguments": json.dumps({"target": "2026-12-25"})}}]},
                    {"content": "32 days until Christmas."}])
    monkeypatch.setattr(cloud_brain.CloudBrain, "_chat", lambda self, m, t=None, on_text=None: next(answers))
    b = cloud_brain.CloudBrain(reg, "groq", "m")
    b.cancel = threading.Event()
    assert b.respond([], reg.schemas(), lambda t, a: True, must_use={"date_math"}) == "32 days until Christmas."
