import pytest

from jarvis.llm.cloud_brain import CloudChain, CloudUnavailable, Router


class FakeCloud:
    def __init__(self, provider, fail=None, tools_before_fail=False):
        self.provider, self.model, self.fail, self.tools_before_fail = provider, "m", fail, tools_before_fail
        self.available, self.last_tool_calls, self.calls = True, [], 0

    def respond(self, messages, tools, confirm):
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
