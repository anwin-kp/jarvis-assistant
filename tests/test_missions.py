"""Missions: checked plans, evidence, recovery, restarts, approvals and privacy (no real models)."""
import json
import threading
import time

import pytest

from jarvis.agent.missions import Mission, Missions, Step, Waiting
from jarvis.tools.registry import Risk, ToolRegistry


class FakeMind:
    """Answers by role; records which calls were private."""

    def __init__(self, plan, judge=None, work=None):
        self.plans = plan if isinstance(plan[0], list) else [plan]
        self.judge, self.work, self.seen = judge or (lambda u: True), work or (lambda msgs: "text"), []

    def chat(self, messages, tools=None, private=False, checkpoint=lambda: None):
        checkpoint()
        head = messages[0]["content"]
        role = "plan" if head.startswith("You plan") else "judge" if head.startswith("You check") else "work"
        self.seen.append((role, private, messages))
        if role == "plan":
            return {"content": json.dumps({"steps": self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]})}
        if role == "judge":
            ok = self.judge(messages[-1]["content"])
            return {"content": json.dumps({"pass": ok, "reason": "because"})}
        out = self.work(messages)
        return out if isinstance(out, dict) else {"content": out}


def use(name, **args):
    return {"content": "", "tool_calls": [{"id": "1", "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def reg():
    r = ToolRegistry()
    r.ran = []

    @r.tool("web_search", "search", {"query": {"type": "string"}}, group="web", read_only=True)
    def web_search(query):
        r.ran.append(("web_search", query))
        return f"found {query}"

    @r.tool("read_email", "email", {"id": {"type": "string"}}, group="email", read_only=True)
    def read_email(id):  # noqa: A002
        r.ran.append(("read_email", id))
        return "secret price 900"

    @r.tool("save_report", "save", {"name": {"type": "string"}, "text": {"type": "string"}}, group="report")
    def save_report(name, text):
        r.ran.append(("save_report", text))
        return "Saved."

    @r.tool("add_task", "task", {"title": {"type": "string"}}, group="reminder")
    def add_task(title):
        r.ran.append(("add_task", title))
        return "Added."

    @r.tool("send_draft", "send", {"id": {"type": "string"}}, group="email", risk=Risk.DANGER)
    def send_draft(id):  # noqa: A002
        r.ran.append(("send_draft", id))
        return "Sent."

    @r.tool("organize_folder", "organize", {"folder": {"type": "string"}}, group="files")
    def organize_folder(folder):
        if not r.ask(f"Move 3 files in {folder}"):
            return "Not approved."
        r.ran.append(("organize_folder", folder))
        return "Moved 3 files."

    @r.tool("click", "ui", {}, group="screen")
    def click():
        return "x"
    return r


def make(reg, tmp_path, mind, **kw):
    ms = Missions(reg, tmp_path, mind, notify=lambda t: ms.said.append(t), **kw)
    ms.said, ms.retry_wait = [], 0
    return ms


def go(ms, goal="research it, check my task list"):
    m = ms.start(goal, background=False)
    ms.run(m)
    return m


CALL = {"id": "s1", "kind": "call", "title": "search", "tool": "web_search", "args": {"query": "laptops"}}


def test_steps_pass_results_on_and_end_done(reg, tmp_path):
    plan = [CALL, {"id": "s2", "kind": "write", "title": "summary", "needs": ["s1"]},
            {"id": "s3", "kind": "call", "title": "save", "tool": "save_report",
             "args": {"name": "r", "text": "{{s2}}"}}]
    ms = make(reg, tmp_path, FakeMind(plan))
    m = go(ms, "research laptops and save a report")
    assert m.status == "done" and reg.ran == [("web_search", "laptops"), ("save_report", "text")]
    assert Mission.load(tmp_path / f"{m.id}.json").status == "done" and "Mission done" in ms.said[-1]
    assert "done" in ms.status_text()


def test_unusable_plans_are_refused_before_anything_runs(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    for bad in ([], [{**CALL, "tool": "click"}], [{**CALL, "args": {}}], [{**CALL, "args": {"query": "{{s9}}"}}],
                [CALL, {"id": "s2", "kind": "call", "title": "t", "tool": "send_draft", "args": {"id": "{{s1}}"}}],
                [CALL, {**CALL, "id": "s2", "args": {"query": "{{s1.results[0].url}}"}}],
                [{**CALL, "args": {"query": "a", "x": 1}}], [CALL, CALL], [{**CALL, "needs": ["s1"]}],
                [{**CALL, "id": f"s{i}"} for i in range(11)]):
        with pytest.raises(ValueError):
            ms._steps(Mission("x", "g"), {"steps": bad})
    m = go(make(reg, tmp_path, FakeMind([{**CALL, "tool": "click"}])))
    assert m.status == "failed" and reg.ran == []


def test_a_failed_step_is_replanned_around(reg, tmp_path):
    reg._tools["web_search"].func = lambda query: "ERROR: down" if query == "laptops" else "found it"
    ms = make(reg, tmp_path, FakeMind([[CALL], [{**CALL, "args": {"query": "other"}}]]))
    m = go(ms)
    assert m.status == "done" and m.replans == 1 and [s.status for s in m.steps] == ["replaced", "done"]
    assert m.steps[1].id == "r1_s1"


def test_endless_failure_stops_and_says_so(reg, tmp_path):
    reg._tools["web_search"].func = lambda query: "ERROR: down"
    ms = make(reg, tmp_path, FakeMind([CALL]))
    m = go(ms)
    assert m.status == "failed" and m.replans == 2 and "Mission failed" in ms.said[-1]


def test_work_needs_a_tool_that_really_succeeded_and_a_review(reg, tmp_path):
    plan = [{"id": "s1", "kind": "work", "title": "find", "tools": ["web_search"], "done_when": "has a name"}]
    turns = []

    def work(msgs):
        turns.append(1)
        return [use("finish", result="invented"), use("click"), use("web_search", query="q"),
                use("finish", result="found q")][len(turns) - 1] if len(turns) <= 4 else use("finish", result="found q")
    verdicts = iter([False, True, True])
    ms = make(reg, tmp_path, FakeMind(plan, judge=lambda u: next(verdicts), work=work))
    m = go(ms)
    assert m.status == "done" and m.steps[0].attempts == 2 and m.steps[0].result == "found q"
    assert reg.ran == [("web_search", "q")]  # the second attempt used what the first one found


def test_the_goal_itself_is_checked(reg, tmp_path):
    plans = [[CALL], [{"id": "s1", "kind": "call", "title": "task", "tool": "add_task", "args": {"title": "t"}}]]
    ms = make(reg, tmp_path, FakeMind(plans, judge=lambda u: "Added." in u))
    m = go(ms)
    assert m.status == "done" and ("add_task", "t") in reg.ran and m.replans == 1


SEND = {"id": "s1", "kind": "call", "title": "send", "tool": "send_draft", "args": {"id": "7"}}


def test_changes_wait_for_the_user_who_sees_the_exact_action(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([SEND]))
    m = go(ms)
    assert m.status == "waiting" and reg.ran == [] and "approve" in ms.said[-1]
    shown, answer = [], [False, True]
    reg.confirm_fn = lambda tool, args: (shown.append((tool.name, tool.risk, args)), answer.pop(0))[1]
    assert "Not approved" in ms.approve() and m.status == "waiting"
    ms._kick = lambda: None
    assert "Approved" in ms.approve()
    assert shown[0][0] == "send_draft" and shown[0][1] is Risk.DANGER and shown[0][2]["id"] == "7"
    ms.run(m)
    assert m.status == "done" and reg.ran == [("send_draft", "7")]  # once


def test_a_question_inside_a_tool_also_waits_and_only_that_exact_one_is_approved(reg, tmp_path):
    plan = [{"id": "s1", "kind": "call", "title": "o", "tool": "organize_folder", "args": {"folder": "D"}}]
    ms = make(reg, tmp_path, FakeMind(plan))
    reg.confirm_fn = lambda tool, args: True
    ms._kick = lambda: None
    m = go(ms)
    for _ in range(2):  # first the tool itself, then the question it asks with its preview
        assert m.status == "waiting" and reg.ran == []
        ms.approve()
        ms.run(m)
    assert m.status == "done" and reg.ran == [("organize_folder", "D")]


def test_skip_never_runs_the_action(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([SEND]), max_replans=0)
    ms._kick = lambda: None
    m = go(ms)
    ms.control("skip")
    ms.run(m)
    assert m.status == "failed" and reg.ran == []


def test_private_data_stays_local_and_going_online_with_it_asks(reg, tmp_path):
    plan = [{"id": "s1", "kind": "call", "title": "mail", "tool": "read_email", "args": {"id": "1"}},
            {"id": "s2", "kind": "write", "title": "w", "needs": ["s1"]},
            {"id": "s3", "kind": "call", "title": "look", "tool": "web_search", "args": {"query": "{{s1}}"}}]
    mind = FakeMind(plan)
    ms = make(reg, tmp_path, mind)
    m = go(ms, "do the thing")
    assert m.status == "waiting" and reg.ran == [("read_email", "1")]
    assert not any("secret price" in str(msgs) and not private for _, private, msgs in mind.seen)
    assert "secret" not in ms.status_text() and "secret" not in " ".join(ms.said)
    reg.confirm_fn = lambda tool, args: "secret price" in args["query"]  # the user sees what would go out
    ms._kick = lambda: None
    ms.approve()
    ms.run(m)
    assert m.status == "done" and reg.ran.count(("web_search", "secret price 900")) == 1
    assert "secret" not in ms.status_text() and "secret" not in " ".join(ms.said)


def test_a_private_goal_is_planned_locally_but_saving_a_report_is_not_private(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    assert ms.start("summarise my unread emails", background=False).private
    assert not ms.start("research laptops, compare them and save a report", background=False).private
    ms.is_private = lambda: True
    assert ms.start("research laptops", background=False).private


def test_a_restart_continues_where_it_stopped(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    m = ms.start("g", background=False)
    m.steps = [Step("s1", "call", "a", "add_task", {"title": "one"}, status="done", result="Added."),
               Step("s2", "call", "b", "web_search", {"query": "x"}, status="running")]
    m.status = "running"
    ms.save(m)
    again = make(reg, tmp_path, FakeMind([CALL]))
    assert again.resume_interrupted() == 1
    end = time.time() + 5
    while again.missions[m.id].status != "done" and time.time() < end:
        time.sleep(0.02)
    assert again.missions[m.id].status == "done" and reg.ran == [("web_search", "x")]


def test_an_interrupted_change_is_not_repeated_blindly(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    m = ms.start("g", background=False)
    m.steps = [Step("s1", "call", "a", "add_task", {"title": "one"}, status="running")]
    m.status, m.inflight = "running", {"step": "s1", "tool": "add_task", "args": {"title": "one"}}
    ms.save(m)
    again = make(reg, tmp_path, FakeMind([CALL]))
    again._kick = lambda: None
    again.resume_interrupted()
    m2 = again.missions[m.id]
    assert m2.status == "waiting" and "may or may not" in again.said[-1] and reg.ran == []
    again.control("skip")  # "it did happen"
    again.run(m2)
    assert m2.status == "done" and reg.ran == []


def test_pause_resume_and_cancel(reg, tmp_path):
    started, release = threading.Event(), threading.Event()
    reg._tools["web_search"].func = lambda query: (started.set(), release.wait(5), "found")[2]
    ms = make(reg, tmp_path, FakeMind([CALL, {**CALL, "id": "s2", "args": {"query": "b"}}]))
    m = ms.start("g")
    assert started.wait(5)
    ms.control("pause")
    release.set()
    end = time.time() + 5
    while m.status != "paused" and time.time() < end:
        time.sleep(0.02)
    assert m.status == "paused"
    ms.control("resume")
    end = time.time() + 5
    while m.status != "done" and time.time() < end:
        time.sleep(0.02)
    assert m.status == "done" and m.steps[0].status == "done"
    assert ms.control("cancel").startswith("ERROR")
    m3 = ms.start("h", background=False)
    ms.control("cancel", m3.id)
    assert m3.status == "cancelled"


def test_budgets_end_a_mission(reg, tmp_path):
    plan = [{"id": "s1", "kind": "work", "title": "f", "tools": ["web_search"]}]
    m = go(make(reg, tmp_path, FakeMind(plan, work=lambda msgs: use("web_search", query="q")), max_calls=5))
    assert m.status == "failed" and m.calls == 5


def test_the_same_change_never_happens_twice(reg, tmp_path):
    plan = [{"id": f"s{i}", "kind": "call", "title": "t", "tool": "add_task", "args": {"title": "x"}} for i in (1, 2)]
    assert go(make(reg, tmp_path, FakeMind(plan))).status == "done" and reg.ran == [("add_task", "x")]


def test_mission_questions_never_reach_the_apps_prompt(reg, tmp_path):
    reg.confirm_fn = lambda *a: pytest.fail("the app's prompt must not open from a background mission")
    ms = make(reg, tmp_path, FakeMind([SEND]))
    with pytest.raises(Waiting):
        m = Mission("x", "g", steps=[Step("s1", "call", "t", "organize_folder", {"folder": "D"})])
        ms._tool(m, m.steps[0], "organize_folder", {"folder": "D"})
    assert go(ms).status == "waiting"
    assert reg.ask.__self__._local.__dict__.get("ask") is None


def test_spoken_mission_commands(reg):
    from jarvis.core.fast_intents import FastIntents

    done = []

    @reg.tool("control_mission", "c", {"action": {"type": "string"}}, group="mission")
    def control_mission(action):
        done.append(action)
        return "ok"
    fi = FastIntents(reg, {})
    for s in ("stop the mission", "resume my mission", "approve the mission", "Skip that mission step."):
        assert fi.try_handle(s, None) == "ok"
    assert done == ["pause", "resume", "approve", "skip"]


def test_not_english_is_heard_again_by_the_better_model(monkeypatch):
    import numpy as np

    from jarvis.stt.groq_stt import GroqSTT

    used = []

    def fake(self, audio, lang, model):
        used.append(model)
        return ("You close it for me." if model == "whisper-large-v3" else self.said), "en"
    monkeypatch.setattr(GroqSTT, "_request", fake)
    stt = GroqSTT("k", languages=["en"])
    stt.said = "Jú, glóði Fómi."
    assert stt.transcribe(np.zeros(16000))[0] == "You close it for me." and len(used) == 2
    stt.said = "Meet me at the café."
    assert stt.transcribe(np.zeros(16000))[0] == stt.said and len(used) == 3


def test_a_real_copy_command_is_not_turned_into_a_project():
    from jarvis.stt.lexicon import Lexicon

    lex = Lexicon()
    lex.add(["Quick OP Mobile"], "project")
    for s in ("copy mobile folder to desktop", "can you copy mobile folder"):
        assert lex.correct(s) == s
    assert lex.correct("Close to copy mobile") == "Close to Quick OP Mobile"


def test_outside_text_cannot_add_actions_to_a_replan(reg, tmp_path):
    evil = {"id": "s1", "kind": "call", "title": "x", "tool": "send_draft", "args": {"id": "1"}}
    ms = make(reg, tmp_path, FakeMind([[CALL], [evil]], judge=lambda u: False))
    m = go(ms)
    assert m.status == "failed" and not any(n == "send_draft" for n, _ in reg.ran)
    with pytest.raises(ValueError, match="doesn't ask"):  # a reminder/to-do the user's words don't ask for
        ms._steps(Mission("x", "research laptops"), {"steps": [
            {"id": "s1", "kind": "call", "title": "t", "tool": "add_task", "args": {"title": "t"}}]})


def test_the_final_review_sees_what_was_really_changed(reg, tmp_path):
    plan = [{"id": "s1", "kind": "write", "title": "Add a task"}]
    seen = []
    ms = make(reg, tmp_path, FakeMind(plan, judge=lambda u: (seen.append(u), "CHANGES" not in u)[1]), max_replans=0)
    assert go(ms).status == "failed" and "(none)" in seen[-1]


def test_impossible_is_only_accepted_after_really_trying(reg, tmp_path):
    reg._tools["web_search"].func = lambda query: "ERROR: not found"
    mind = FakeMind([CALL])
    plans, real = iter([{"steps": [CALL]}] + [{"impossible": "no such page"}] * 2), mind.chat
    mind.chat = lambda msgs, *a, **k: ({"content": json.dumps(next(plans))}
                                       if msgs[0]["content"].startswith("You plan") else real(msgs, *a, **k))
    m = go(make(reg, tmp_path, mind))
    assert m.status == "failed" and "no such page" in m.note
    with pytest.raises(ValueError, match="trying"):
        make(reg, tmp_path, mind)._steps(Mission("x", "g"), {"impossible": "why"})


def test_one_odd_review_does_not_throw_finished_work_away(reg, tmp_path):
    verdicts = iter([False, True, True])
    ms = make(reg, tmp_path, FakeMind([CALL], judge=lambda u: next(verdicts)))
    m = go(ms)
    assert m.status == "done" and m.replans == 0


def test_what_the_user_plainly_asked_for_must_be_planned_and_happen(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    m = Mission("x", "research laptops and add a to-do to buy one")
    with pytest.raises(ValueError, match="add_task"):
        ms._steps(m, {"steps": [CALL]})
    assert "add_task" in ms._goal_gap(m)


def test_a_value_to_pick_out_of_a_result_becomes_a_work_step(reg, tmp_path):
    ms = make(reg, tmp_path, FakeMind([CALL]))
    s = ms._steps(Mission("x", "g"), {"steps": [CALL, {"id": "s2", "kind": "call", "title": "t", "tool": "read_email",
                                                       "args": {"id": "{{s1}}"}}]})[1]
    assert (s.kind, s.tools, s.needs, s.args) == ("work", ["read_email"], ["s1"], {})
