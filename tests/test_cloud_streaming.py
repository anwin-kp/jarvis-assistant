"""Cloud replies are streamed: speech starts with the first sentence, tool calls still work."""

import json
import threading
import types

import requests

from jarvis.llm import cloud_brain
from jarvis.tools.registry import ToolRegistry


def sse(*chunks, done=True, drop=False):
    lines = [f"data: {json.dumps({'choices': [{'delta': c}]})}" for c in chunks] + (["data: [DONE]"] if done else [])

    def iter_lines(decode_unicode=True):
        for ln in lines:
            yield ln
            yield ""
        if drop:
            raise requests.ConnectionError("dropped")

    return types.SimpleNamespace(status_code=200, ok=True, headers={}, text="", iter_lines=iter_lines,
                                 encoding=None)


def brain(monkeypatch, responses, reg=None):
    it = iter(responses)
    bodies = []
    monkeypatch.setattr(cloud_brain.requests, "post", lambda url, json=None, **k: (bodies.append(json), next(it))[1])
    b = cloud_brain.CloudBrain(reg or ToolRegistry(), "groq", "m")
    b.cancel = threading.Event()
    return b, bodies


def test_sentences_are_spoken_while_the_reply_streams(monkeypatch):
    b, bodies = brain(monkeypatch, [sse({"content": "Kochi is warm today. "}, {"content": "Expect rain "},
                                        {"content": "after 4 PM."})])
    spoken = []
    reply = b.respond([{"role": "user", "content": "weather?"}], [], None, on_sentence=spoken.append)
    assert reply == "Kochi is warm today. Expect rain after 4 PM."
    assert spoken == ["Kochi is warm today.", "Expect rain after 4 PM."] and bodies[0]["stream"] is True


def test_streamed_tool_calls_are_joined_and_run(monkeypatch):
    reg, ran = ToolRegistry(), []

    @reg.tool("get_weather", "w", {"city": {"type": "string"}})
    def get_weather(city):
        ran.append(city)
        return "28 C, sunny"

    call = [{"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": '{"ci'}},
            {"index": 0, "function": {"arguments": 'ty": "Kochi"}'}}]
    b, _ = brain(monkeypatch, [sse({"tool_calls": [call[0]]}, {"tool_calls": [call[1]]}),
                               sse({"content": "It's 28 degrees and sunny in Kochi."})], reg)
    spoken = []
    assert b.respond([], reg.schemas(), lambda t, a: True, on_sentence=spoken.append).startswith("It's 28")
    assert ran == ["Kochi"] and spoken == ["It's 28 degrees and sunny in Kochi."]


def test_a_dropped_stream_keeps_what_was_already_said(monkeypatch):
    b, _ = brain(monkeypatch, [sse({"content": "The first part arrived. "}, done=False, drop=True)])
    spoken = []
    assert b.respond([], [], None, on_sentence=spoken.append) == "The first part arrived."
    assert spoken == ["The first part arrived."]


def test_without_a_listener_nothing_changes(monkeypatch):
    resp = types.SimpleNamespace(status_code=200, ok=True, headers={}, text="",
                                 json=lambda: {"choices": [{"message": {"content": "Hi."}}]})
    b, bodies = brain(monkeypatch, [resp])
    assert b.respond([], [], None) == "Hi." and "stream" not in bodies[0]


def test_stream_text_is_read_as_utf8(monkeypatch):
    resp = sse({"content": "It's 28°C – sunny."})
    b, _ = brain(monkeypatch, [resp])
    assert b.respond([], [], None, on_sentence=lambda s: None) == "It's 28°C – sunny." and resp.encoding == "utf-8"
