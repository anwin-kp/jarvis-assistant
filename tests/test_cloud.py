import json

import jarvis.llm.cloud_brain as cb
from jarvis.llm.cloud_brain import CloudBrain, CloudUnavailable, Router
from jarvis.tools.registry import ToolRegistry

OK = lambda t, a: True


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.ok, self.text = status, data, status < 400, json.dumps(data)

    def json(self):
        return self._data


def make_reg():
    reg = ToolRegistry()

    @reg.tool("get_time", "time", {})
    def get_time():
        return "12:00"

    @reg.tool("read_secret_file", "file", {}, group="files")
    def read_secret_file():
        return "secret"

    return reg


class FakeLocal:
    model = "local-model"
    last_tool_calls: list = []

    def respond(self, messages, tools, confirm, **kw):
        return "local answer"


def test_cloud_tool_loop(monkeypatch):
    replies = iter([
        Resp(200, {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]}}]}),
        Resp(200, {"choices": [{"message": {"content": "It's 12:00."}}]}),
    ])
    sent = []
    monkeypatch.setattr(cb.requests, "post", lambda url, json, **kw: sent.append(json) or next(replies))
    monkeypatch.setattr(cb, "get_secret", lambda name: "key")
    reg = make_reg()
    brain = CloudBrain(reg, "groq", "m")
    assert brain.respond([{"role": "user", "content": "time?"}], reg.schemas(), OK) == "It's 12:00."
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "12:00"}


def test_router_falls_back_and_keeps_private_tools_local(monkeypatch):
    monkeypatch.setattr(cb, "get_secret", lambda name: "key")
    reg = make_reg()
    cloud = CloudBrain(reg, "groq", "m")
    calls = []

    def cloud_respond(m, t, c):
        calls.append(1)
        raise CloudUnavailable("429")

    cloud.respond = cloud_respond
    r = Router(FakeLocal(), cloud, reg, mode="cloud_first")
    assert r.respond([], reg.schemas({"core"}), OK) == "local answer" and calls == [1]  # fell back
    assert r.respond([], reg.schemas(), OK) == "local answer" and calls == [1]  # files tool: never cloud
    r.private = True
    assert not r._use_cloud(reg.schemas({"core"}))


def test_router_local_mode_without_key(monkeypatch):
    monkeypatch.setattr(cb, "get_secret", lambda name: None)
    reg = make_reg()
    r = Router(FakeLocal(), CloudBrain(reg, "groq", "m"), reg, mode="cloud_first")
    assert not r._use_cloud(reg.schemas({"core"})) and r.model == "local-model"


def test_smart_mode_routes_by_intent_and_hides_private_tools(monkeypatch):
    monkeypatch.setattr(cb, "get_secret", lambda name: "key")
    reg = make_reg()
    cloud = CloudBrain(reg, "groq", "m")
    seen = []
    cloud.respond = lambda m, tools, c, **kw: seen.append([t["function"]["name"] for t in tools]) or "cloud answer"
    r = Router(FakeLocal(), cloud, reg, mode="smart")
    all_tools = reg.schemas()  # includes the private files tool, offered "just in case"
    assert r.respond([], all_tools, OK, think=True, private=False) == "cloud answer"
    assert seen == [["get_time"]]  # the cloud never receives private tools
    assert r.respond([], all_tools, OK, think=True, private=True) == "local answer"  # you asked for your files
    assert r.respond([], all_tools, OK, think=False, private=False) == "local answer"  # easy: stays local
