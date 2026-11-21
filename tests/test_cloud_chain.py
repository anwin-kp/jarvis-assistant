import pytest

from jarvis.llm.cloud_brain import CloudChain, CloudUnavailable, Router


class FakeCloud:
    def __init__(self, provider, fail=None, tools_before_fail=False):
        self.name = provider
        self.provider, self.model, self.fail, self.tools_before_fail = provider, "m", fail, tools_before_fail
        self.available, self.last_tool_calls, self.calls = True, [], 0

    def respond(self, messages, tools, confirm, prior_tools=None):
        self.calls += 1
        self.last_tool_calls = ["spotify_play"] if self.tools_before_fail else []
        if self.fail:
            raise CloudUnavailable(self.fail)
        return f"reply from {self.provider}"


def test_falls_back_when_the_first_is_rate_limited_and_skips_it_for_a_while():
    groq, gemini = FakeCloud("groq", fail="groq HTTP 429"), FakeCloud("gemini")
    chain = CloudChain([groq, gemini])
    assert chain.respond([], [], None) == "reply from gemini"
    assert chain.respond([], [], None) == "reply from gemini" and groq.calls == 1  # cooling down


def test_never_retries_elsewhere_after_a_tool_ran():
    groq, gemini = FakeCloud("groq", fail="network", tools_before_fail=True), FakeCloud("gemini")
    with pytest.raises(CloudUnavailable):
        CloudChain([groq, gemini]).respond([], [], None)
    assert gemini.calls == 0


def test_smart_mode_sends_actions_to_the_cloud_but_keeps_private_requests_local():
    class Reg:
        def get(self, name):
            return None

    r = Router(local=None, cloud=CloudChain([FakeCloud("groq")]), registry=Reg(), mode="smart")
    assert r._use_cloud([], action=True, private=False)
    assert not r._use_cloud([], action=True, private=True)
    assert not r._use_cloud([], action=False, private=False)  # plain chat stays local


class ToolThenLimit:
    """Runs a tool, then hits the per-minute limit on the next step (what happened with 'Close Spotify')."""

    def __init__(self, provider):
        self.name = provider
        self.provider, self.model, self.available, self.last_tool_calls, self.seen = provider, "m", True, [], None

    def respond(self, messages, tools, confirm, prior_tools=None):
        self.last_tool_calls = ["close_app"]
        e = CloudUnavailable("groq HTTP 429")
        e.partial = messages + [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "close_app",
                                                                                         "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "Closed spotify (1 process)."}]
        e.results = ["Closed spotify (1 process)."]
        raise e


class Finisher(FakeCloud):
    def respond(self, messages, tools, confirm, prior_tools=None):
        self.seen, self.prior = messages, prior_tools
        self.last_tool_calls = list(prior_tools or [])
        return "Spotify is closed."


def test_the_next_provider_continues_from_the_tool_results_without_rerunning():
    gemini = Finisher("gemini")
    chain = CloudChain([ToolThenLimit("groq"), gemini])
    assert chain.respond([{"role": "user", "content": "close spotify"}], [], None) == "Spotify is closed."
    assert gemini.prior == ["close_app"] and "do NOT run these again" in gemini.seen[-1]["content"]
    assert chain.last_tool_calls == ["close_app"]


def test_all_providers_busy_after_a_tool_ran_answers_with_the_result():
    from jarvis.llm.cloud_brain import result_reply

    chain = CloudChain([ToolThenLimit("groq")])
    with pytest.raises(CloudUnavailable) as e:
        chain.respond([], [], None)
    assert result_reply(e.value.results) == "Closed spotify (1 process)."
    assert result_reply(["ERROR: no such app"]).startswith("That didn't work")


def test_handover_history_is_plain_text():
    from jarvis.llm.cloud_brain import as_text_history

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "close spotify"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "close_app",
                                                                                           "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "Closed spotify."}]
    out = as_text_history(msgs)
    assert [m["role"] for m in out] == ["system", "user", "user"] and "do NOT run these again" in out[-1]["content"]
    assert "close_app" in out[-1]["content"] and "Closed spotify." in out[-1]["content"]


def test_each_model_cools_down_separately_and_for_as_long_as_asked():
    a, b = FakeCloud("groq", fail="429"), FakeCloud("groq")
    a.name, b.name = "groq/big", "groq/small"
    chain = CloudChain([a, b])
    assert chain.respond([], [], None) == "reply from groq"  # the other Groq model answered
    assert "groq/small" not in chain.cooldown and "groq/big" in chain.cooldown


def test_short_commands_are_not_mistaken_for_echo():
    from jarvis.__main__ import echo_of

    assert not echo_of("Close Spotify", ["Opening spotify."])
    assert echo_of("You have no", ["You have no unread emails."])


def test_rate_limit_waits_only_as_long_as_groq_says(monkeypatch):
    import types

    from jarvis.llm import cloud_brain

    resp = types.SimpleNamespace(status_code=429, headers={"retry-after": "4"}, ok=False, text="")
    monkeypatch.setattr(cloud_brain.requests, "post", lambda *a, **k: resp)
    b = cloud_brain.CloudBrain(None, "groq", "openai/gpt-oss-20b")
    with pytest.raises(CloudUnavailable) as e:
        b._chat([], None)
    assert e.value.retry_after == 4 and b.name == "groq/openai/gpt-oss-20b"
