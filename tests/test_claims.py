import pytest

from jarvis.llm.ollama_brain import CLAIMS_ACTION

CLAIMS = [
    "I've paused the music.",
    "Done, I've saved that to your notes.",
    "I have set a timer for 5 minutes.",
    "Your name is now permanently saved as Anwin.",
    "The reminder has been scheduled for 6 pm.",
    "I just sent the email to Priya.",
    "Spotify has been opened.",
    "I turned the volume up.",
    "Successfully deleted the file.",
    "12 times 7 is 84. The display shows 84.",
]
PROSE = [  # real false alarms from the log, and ordinary answers
    "The history of Kerala dates back to the 3rd century BCE, when it was written about by Greek travellers.",
    "Kerala's history dates back over 3,000 years, with evidence of Neolithic settlements that have been studied.",
    "1. Kerala is renowned for its backwater houseboats, which offer a relaxed way to see the villages.",
    "The First World War started in 1914 and ended in 1918.",
    "Photosynthesis is the process by which plants convert light into energy that is stored as sugar.",
    "The playlist Mix-Up Mash has 42 songs.",
    "Would you like me to open Spotify?",
    "I can set a timer if you like.",
]


@pytest.mark.parametrize("text", CLAIMS)
def test_detects_action_claims(text):
    assert CLAIMS_ACTION.search(text)


@pytest.mark.parametrize("text", PROSE)
def test_ignores_ordinary_prose(text):
    assert not CLAIMS_ACTION.search(text), CLAIMS_ACTION.search(text)


@pytest.mark.parametrize("text", ["I'll check the clipboard for you. One moment please.",
                                  "I'll dim the screen for you.",
                                  "Let me look that up.", "I will lower the volume now."])
def test_promises_without_a_tool_get_a_nudge(text):
    from jarvis.llm.ollama_brain import NUDGE_PROMISE, unbacked_claim

    assert unbacked_claim(text) == NUDGE_PROMISE


@pytest.mark.parametrize("text", PROSE + ["Let me know if you need anything else.", "I'll be here if you need me."])
def test_no_nudge_for_ordinary_answers(text):
    from jarvis.llm.ollama_brain import unbacked_claim

    assert unbacked_claim(text) is None


def test_promise_after_a_tool_still_gets_a_nudge_but_a_true_claim_does_not():
    from jarvis.llm.ollama_brain import NUDGE_PROMISE, unbacked_claim

    assert unbacked_claim("I've opened Spotify. Let me play your playlist now. One moment, please.",
                          tools_ran=True) == NUDGE_PROMISE
    assert unbacked_claim("I've opened Spotify.", tools_ran=True) is None


def test_website_addresses_offer_the_browser():
    from jarvis.llm.tool_groups import pick_groups

    assert "browser" in pick_groups("open amazon.in and search for a usb c cable")
    assert "browser" in pick_groups("go to github.com/anwin-kp")
    assert "browser" not in pick_groups("what's the weather like")


def test_a_read_only_tool_does_not_back_a_change_claim():
    from jarvis.llm.ollama_brain import NUDGE, acted, unbacked_claim
    from jarvis.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.tool("list_unread_emails", "list", {}, read_only=True)(lambda: "3 unread")
    reg.tool("set_volume", "vol", {})(lambda: "ok")
    claim = "I've marked all your emails as read."
    assert unbacked_claim(claim, acted(reg, ["list_unread_emails"])) == NUDGE
    assert unbacked_claim(claim, acted(reg, ["set_volume"])) is None
