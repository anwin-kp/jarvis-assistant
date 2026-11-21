"""Regression tests for the November 2026 audit fixes."""

import queue
import threading
import types

import pytest

from jarvis.llm.cloud_brain import CloudUnavailable, Router
from jarvis.tools.registry import Risk, Tool, ToolRegistry

# ------------------------------------------------------------------ media: play / pause check what's playing


@pytest.fixture
def keys(monkeypatch):
    from jarvis.tools import media_tools

    pressed = []
    monkeypatch.setattr(media_tools, "press_key", pressed.append)
    return pressed


def sessions_of(*found):
    sent = []

    def sessions(command=None):
        if command:
            sent.append(command)
            return [{**found[0], "sent": True}]
        return list(found)

    return sessions, sent


def test_pause_when_nothing_plays_does_not_start_music(keys):
    from jarvis.tools.media_tools import play_or_pause

    sessions, sent = sessions_of({"app": "Spotify", "status": "paused", "title": "Song", "artist": "X"})
    assert play_or_pause("pause", sessions) == "Nothing is playing."
    assert not keys and not sent
    assert play_or_pause("pause", lambda command=None: []) == "Nothing is playing."


def test_play_while_playing_does_not_pause_it(keys):
    from jarvis.tools.media_tools import play_or_pause

    sessions, sent = sessions_of({"app": "Spotify", "status": "playing", "title": "Song", "artist": "X"})
    assert play_or_pause("play", sessions) == "Already playing Song by X (Spotify)."
    assert not keys and not sent
    assert play_or_pause("pause", sessions) == "Paused Song by X (Spotify)." and sent == ["pause"] and not keys


def test_without_media_controls_the_toggle_key_is_used(keys):
    from jarvis.tools.media_tools import KEYS, play_or_pause

    assert play_or_pause("pause", lambda command=None: None) == "Paused."
    assert keys == [KEYS["play_pause"]]


# ------------------------------------------------------------------ approval prompt keeps other events


def test_approval_prompt_keeps_events_that_arrive_meanwhile():
    from jarvis.__main__ import App

    q = queue.Queue()
    me = types.SimpleNamespace(q=q, tray=None, _publish=lambda *a, **k: None, _say=lambda t: None)
    for e in [("reminder", "tea"), ("heard", "hello", False, 0.1), ("text", "y")]:
        q.put(e)
    tool = Tool("close_app", "", {}, Risk.CONFIRM, "system", lambda: None)
    assert App.confirm(me, tool, {"name": "x"}) is True
    assert [q.get_nowait(), q.get_nowait()] == [("reminder", "tea"), ("heard", "hello", False, 0.1)]
    assert q.empty()


# ------------------------------------------------------------------ "stop" ends a multi-step turn


def fake_call(name):
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments={}))


def test_stop_ends_a_local_tool_chain():
    from jarvis.llm.ollama_brain import OllamaBrain

    reg, ran = ToolRegistry(), []
    brain = OllamaBrain.__new__(OllamaBrain)
    brain.reg, brain.model, brain.think, brain.keep_alive, brain.options, brain.max_steps = reg, "m", False, 0, {}, 6
    brain.cancel = threading.Event()

    @reg.tool("click", "click", {})
    def click():
        ran.append("click")
        brain.cancel.set()  # the user says "stop" while the first click runs
        return "clicked"

    msg = types.SimpleNamespace(content="", tool_calls=[fake_call("click"), fake_call("click")])
    brain.client = types.SimpleNamespace(chat=lambda **kw: types.SimpleNamespace(message=msg))
    assert brain.respond([{"role": "user", "content": "x"}], reg.schemas(), lambda t, a: True) == \
        "Stopped (already done: click)."
    assert ran == ["click"]


def test_empty_reply_after_tools_says_the_result():
    from jarvis.llm.ollama_brain import OllamaBrain

    reg = ToolRegistry()
    brain = OllamaBrain.__new__(OllamaBrain)
    brain.reg, brain.model, brain.think, brain.keep_alive, brain.options, brain.max_steps = reg, "m", False, 0, {}, 6
    brain.cancel = threading.Event()

    @reg.tool("calc", "calc", {})
    def calc():
        return "The display shows 84."

    replies = iter([types.SimpleNamespace(content="", tool_calls=[fake_call("calc")]),
                    types.SimpleNamespace(content="", tool_calls=None)])
    brain.client = types.SimpleNamespace(chat=lambda **kw: types.SimpleNamespace(message=next(replies)))
    assert brain.respond([], reg.schemas(), lambda t, a: True) == "The display shows 84."


# ------------------------------------------------------------------ cloud errors


def test_a_bad_request_tries_the_next_model_without_cooldown(monkeypatch):
    from jarvis.llm import cloud_brain

    resp = types.SimpleNamespace(status_code=400, headers={}, ok=False, text="tool_use_failed")
    monkeypatch.setattr(cloud_brain.requests, "post", lambda *a, **k: resp)
    with pytest.raises(CloudUnavailable) as e:
        cloud_brain.CloudBrain(None, "groq", "m")._chat([], None)
    assert e.value.retry_after == 0


def test_a_garbled_cloud_answer_does_not_crash_the_turn(monkeypatch):
    from jarvis.llm import cloud_brain

    resp = types.SimpleNamespace(status_code=200, headers={}, ok=True, text="", json=lambda: {"error": "x"})
    monkeypatch.setattr(cloud_brain.requests, "post", lambda *a, **k: resp)
    with pytest.raises(CloudUnavailable):
        cloud_brain.CloudBrain(None, "groq", "m")._chat([], None)


class DeadLocal:
    model, last_tool_calls = "local", []

    def __init__(self):
        self.cancel = threading.Event()

    def respond(self, *a, **k):
        self.last_tool_calls = []
        raise ConnectionError("Ollama is not running")


class Cloud:
    available, provider, model, last_tool_calls = True, "groq", "m", []

    def respond(self, messages, tools, confirm):
        return "from the cloud"


def test_local_model_down_answers_from_the_cloud():
    reg = ToolRegistry()
    r = Router(DeadLocal(), Cloud(), reg, mode="smart")
    assert r.respond([{"role": "user", "content": "hi"}], [], None) == "from the cloud"
    assert r.last_backend == "cloud"


def test_local_model_down_never_sends_a_private_turn_to_the_cloud():
    r = Router(DeadLocal(), Cloud(), ToolRegistry(), mode="smart")
    with pytest.raises(ConnectionError):
        r.respond([{"role": "user", "content": "read my email"}], [], None, private=True)
    r.private = True  # /private
    with pytest.raises(ConnectionError):
        r.respond([{"role": "user", "content": "hi"}], [], None)


def test_the_router_shares_one_stop_switch():
    local, cloud = DeadLocal(), Cloud()
    Router(local, cloud, ToolRegistry())
    assert cloud.cancel is local.cancel


# ------------------------------------------------------------------ smaller fixes


def test_powershell_dotnet_calls_and_subexpressions_are_blocked():
    from jarvis.tools.shell_tools import check_command

    for cmd in ["Get-Date -Format ([Diagnostics.Process]::Start('calc'))", "Get-Content $env:USERPROFILE\\x",
                "Get-ChildItem -Path {calc}", "Get-Process @args"]:
        assert check_command(cmd), cmd
    assert check_command("Get-Process") is None and check_command("ipconfig /all") is None


def test_news_with_dead_feeds_answers_quickly(monkeypatch):
    import requests

    from jarvis.tools import web_tools

    def down(*a, **k):
        assert k.get("timeout"), "every feed download needs a timeout"
        raise requests.ConnectionError("down")

    monkeypatch.setattr(web_tools.requests, "get", down)
    reg = ToolRegistry()
    web_tools.register(reg, ["https://a.example/rss", "https://b.example/rss"])
    assert reg.get("news_headlines").func().startswith("ERROR")


def test_learning_a_fact_embeds_only_new_texts(tmp_path):
    import numpy as np

    from jarvis.memory.store import Memory

    m, seen = Memory(tmp_path / "m.db"), []

    def embed(texts):
        seen.extend(texts)
        return [np.array([len(t), 1.0, t.count("a")]) for t in texts]

    m.embed = embed
    for f in ["[food] Loves biryani.", "[music] Likes A R Rahman.", "[work] Works at a startup."]:
        m.learn(f)
    assert len(seen) == len(set(seen))  # nothing was embedded twice
