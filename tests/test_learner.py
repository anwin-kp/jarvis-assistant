import json

from jarvis.core.orchestrator import PERSONAL_END, PERSONAL_START
from jarvis.llm.cloud_brain import personal_context
from jarvis.memory.learner import Learner, clean_fact, flatten, supported
from jarvis.memory.store import Memory


def test_clean_fact_categories_and_secrets():
    assert clean_fact("[Music] Likes Yuvan.") == "[music] Likes Yuvan."
    assert clean_fact("identity: Is called Anwin") == "[identity] Is called Anwin"
    assert clean_fact("Likes tea a lot") == "[other] Likes tea a lot"
    assert clean_fact("[weird] Has a cat named Leo") == "[other] Has a cat named Leo"
    assert clean_fact("[identity] Email is someone@example.com") is None
    assert clean_fact("[identity] Phone is +91 98765 43210") is None
    assert clean_fact(["nested"]) is None


def test_flatten_nested_model_output():
    assert flatten(["a", ["b", ["c"]], {"fact": "d"}]) == ["a", "b", "c", {"fact": "d"}]
    assert flatten(None) == []


def test_evidence_rules():
    said = "What is the weather today at Goa? I'm in Goa, India. Can you see my screen? Play my mix-up mash playlist"
    assert supported({"fact": "[places] Is in Goa, India.", "evidence": "I'm in Goa, India."}, said)
    # asking about a place says nothing about the user (no first person)
    assert not supported({"fact": "[places] Lives in Goa.", "evidence": "What is the weather today at Goa?"}, said)
    # a real user line, but unrelated to the fact
    assert not supported({"fact": "[preferences] Prefers Spotify.", "evidence": "Can you see my screen?"}, said)
    # invented quote
    assert not supported({"fact": "[people] Colleague Kishore.", "evidence": "my friend Kishore"}, said)
    assert supported({"fact": "[music] Likes the Mix-Up Mash playlist.", "evidence": "Play my mix-up mash"}, said)


def test_learner_reads_new_turns_once_and_only_from_the_user(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.log_turn("I'm vegetarian and my sister Anu lives in Kochi", "Noted!")
    mem.log_turn("what's the weather in Delhi", "Sunny, 30 degrees.")
    seen = []

    def chat(prompt):
        seen.append(prompt)
        return json.dumps({"add": [
            {"fact": "[food] Is vegetarian.", "evidence": "I'm vegetarian"},
            {"fact": "[people] Has a sister named Anu who lives in Kochi.", "evidence": "my sister Anu lives in Kochi"},
            {"fact": "[places] Lives in Delhi.", "evidence": "what's the weather in Delhi"},
        ]})

    learner = Learner(mem, chat, lambda: False)
    assert learner.step() and not learner.step()  # second call: nothing new
    facts = [f["fact"] for f in mem.list_facts()]
    assert facts == ["[food] Is vegetarian.", "[people] Has a sister named Anu who lives in Kochi."]
    assert "User: I'm vegetarian" in seen[0] and "(JARVIS replied: Noted!)" in seen[0]


def test_personal_context_is_dropped_for_the_cloud_and_unwrapped_locally():
    sys_msg = {"role": "system", "content": f"Persona.\n\n{PERSONAL_START}\n- [food] Is vegetarian.\n{PERSONAL_END}"}
    msgs = [sys_msg, {"role": "user", "content": "hi"}]
    cloud = personal_context(msgs, keep=False)
    local = personal_context(msgs, keep=True)
    assert cloud[0]["content"] == "Persona." and "vegetarian" not in cloud[0]["content"]
    assert local[0]["content"] == "Persona.\n\n- [food] Is vegetarian." and PERSONAL_START not in local[0]["content"]
    assert cloud[1] == msgs[1]


def test_profile_keeps_core_and_adds_relevant(tmp_path):
    mem = Memory(tmp_path / "m.db")
    for i in range(30):
        mem.remember(f"[other] Trivia number {i}.")
    mem.remember("[identity] Is called Anwin.")
    for i in range(10):
        mem.remember(f"[music] Song fact {i}.")
    prof = mem.profile("", core=5, relevant=5)
    assert "[identity] Is called Anwin." in prof and len(prof.splitlines()) <= 10


def test_a_correction_becomes_a_lesson(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.log_turn("check my email", "You have 5 unread emails. 1. Google Security Alert ...")
    mem.log_turn("No, next time when I say check my email you need to list only the important ones", "Got it.")

    def chat(prompt):
        assert "LESSONS" in prompt
        return json.dumps({"add": [{"fact": "[lessons] When Anwin says 'check my email', list only the important ones.",
                                    "evidence": "next time when I say check my email you need to list only the "
                                                "important ones"}]})

    Learner(mem, chat, lambda: False).step()
    assert mem.list_facts()[0]["fact"].startswith("[lessons]")
    assert "[lessons]" in mem.profile("anything", core=1, relevant=0)


def test_weekly_review_runs_once_a_week(monkeypatch, tmp_path):
    from jarvis import review

    monkeypatch.setattr(review, "LAST_RUN", tmp_path / "last-run.txt")
    fake = [{"signals": ["tool_error"]}, {"signals": ["stopped"]}, {"signals": []}]
    monkeypatch.setattr(review, "turns", lambda days: fake)
    monkeypatch.setattr(review, "report", lambda days: review.DATA / "review" / "r.md")
    first = review.run_weekly()
    assert first.startswith("Weekly self-check: 3 turns, 1 went wrong (tool error 1)")
    assert review.run_weekly() is None  # not due again for a week
