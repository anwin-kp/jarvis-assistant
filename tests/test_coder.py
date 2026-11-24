"""The coding agent, with a scripted model (no network): branch + worktree, safe edits, commit only its files."""

import json
import subprocess

import pytest

from jarvis.agent.coder import Coder, Workspace, compact, git
from jarvis.tools.code_tools import Project


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "app"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / ".env").write_text("KEY=1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    git(r, "config", "user.name", "t")  # a real repo has an identity (CI has none: merges failed there)
    git(r, "config", "user.email", "t@t")
    git(r, "add", "calc.py")
    git(r, "commit", "-qm", "init", check=True)
    (r / "notes.txt").write_text("mine, uncommitted\n")
    return r


def script(monkeypatch, steps):
    """The model's replies, one per request: [(tool, args), ...]."""
    it = iter(steps)

    def chat(self, messages, tools=None, on_text=None):
        name, args = next(it)
        return {"content": "", "tool_calls": [{"id": f"c{name}", "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}

    monkeypatch.setattr("jarvis.llm.cloud_brain.CloudBrain._chat", chat)
    monkeypatch.setattr("jarvis.llm.cloud_brain.CloudBrain.available", property(lambda self: True))


def test_the_agent_works_on_its_own_branch_and_commits_only_its_edit(tmp_path, repo, monkeypatch):
    script(monkeypatch, [("read_file", {"path": "calc.py", "line_end": 9}),  # an invented argument is ignored
                         ("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"}),
                         ("write_file", {"path": "__pycache__/x.pyc", "content": ""}),  # wrote it: committed
                         ("finish", {"summary": "Fixed add()."})])
    said = []
    c = Coder([{"provider": "groq", "model": "m"}], tmp_path / "data", notify=said.append)
    (repo / "junk.log").write_text("from a check")  # created by a check, not by the agent: never committed
    t = c.start(Project("app", repo, []), "fix add", background=False)
    assert t.status == "done" and t.files == ["__pycache__/x.pyc", "calc.py"] and "Fixed add()." in said[0]
    assert git(repo, "branch", "--show-current") == "main" and "a - b" in (repo / "calc.py").read_text()
    assert (repo / "notes.txt").exists() and not (tmp_path / "data" / "code-worktrees" / t.id).exists()
    shown = git(repo, "show", "--name-only", "--format=", t.branch).splitlines()
    assert sorted(shown) == ["__pycache__/x.pyc", "calc.py"]
    assert "-    return a - b" in c.changes(t)
    assert c.merge(t).startswith("Merged") and "a + b" in (repo / "calc.py").read_text()  # untracked notes: fine
    assert (repo / "notes.txt").exists() and not (repo / "junk.log").read_text() == ""


def test_secrets_and_escapes_are_refused(tmp_path, repo):
    ws = Workspace(repo, Project("app", repo, []))
    for bad in (".env", "../outside.txt", ".git/config"):
        with pytest.raises(PermissionError):
            ws._path(bad)
    assert ws.replace_in_file("calc.py", "return", "x").startswith("ERROR") is False  # unique: fine
    assert ws.replace_in_file("calc.py", "a", "b").startswith("ERROR")  # not unique: refused


def test_old_tool_results_are_trimmed():
    msgs = [{"role": "tool", "content": "x" * 1000} for _ in range(6)]
    out = compact(msgs, keep=2, limit=100)
    assert [len(m["content"]) for m in out[:4]] == [111] * 4 and len(out[-1]["content"]) == 1000


def test_a_merge_that_fails_is_never_reported_as_merged(tmp_path, repo, monkeypatch):
    script(monkeypatch, [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"}),
                         ("finish", {"summary": "Fixed."})])
    c = Coder([{"provider": "groq", "model": "m"}], tmp_path / "data")
    t = c.start(Project("app", repo, []), "fix", background=False)
    (repo / ".git" / "index.lock").write_text("")  # git is busy: the merge can't run
    out = c.merge(t)
    assert out.startswith("ERROR") and "a - b" in (repo / "calc.py").read_text()
    (repo / ".git" / "index.lock").unlink()
    assert c.merge(t).startswith("Merged")


# ------------------------------------------------------------------ the developer loop


def py_repo(tmp_path, sub=""):
    """A real little Python project (with pytest) that has a bug; optionally in a folder inside the repo."""
    import sys

    top = tmp_path / "top"
    r = top / sub if sub else top
    r.mkdir(parents=True)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef one():\n    return 1\n")
    (r / "test_calc.py").write_text("from calc import one\n\n\ndef test_one():\n    assert one() == 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(top)], check=True)
    git(top, "add", "-A")
    git(top, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", check=True)
    return Project("app", r, []), {"cmds": {"test": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]}}


def develop(monkeypatch, tmp_path, project, cmds, steps, reviews, **kw):
    """steps: the agent's tool calls; reviews: what the reviewer answers, in order."""
    agent, review = iter(steps), iter(reviews)

    def chat(self, messages, tools=None, on_text=None):
        if not tools:
            return {"content": next(review)}
        name, args = next(agent)
        return {"content": "", "tool_calls": [{"id": "c", "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}
    monkeypatch.setattr("jarvis.llm.cloud_brain.CloudBrain._chat", chat)
    monkeypatch.setattr("jarvis.llm.cloud_brain.CloudBrain.available", property(lambda self: True))
    monkeypatch.setattr("jarvis.agent.coder.dev_commands", lambda p, extra=None: dict(cmds["cmds"]))
    c = Coder([{"provider": "groq", "model": "m"}], tmp_path / "data", **kw)
    return c, c.start(project, "fix add: it subtracts", background=False)


FIX = ("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})
GOOD_TEST = ("write_file", {"path": "test_add.py", "content": "from calc import add\n\n\ndef test_add():\n"
                                                              "    assert add(2, 3) == 5\n"})
DONE = ("finish", {"summary": "add() adds now."})
OK, NO = '{"approve": true, "problems": []}', '{"approve": false, "problems": ["add a test"]}'


def test_a_fix_is_verified_by_tests_review_and_a_test_that_fails_without_it(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    c, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE], [OK])
    assert t.verified and t.rounds == 1 and "fails without the change" in " ".join(t.proof), (t.problems, t.summary)
    assert "a + b" in git(p.path, "show", f"{t.branch}:calc.py")  # put back after trying without it
    assert "Verified" in c.report(t) and "fails without" in c.pr_text(t)[1]


def test_an_app_inside_a_repo_works(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path, "apps/mobile")
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE], [OK])
    assert t.verified, (t.problems, t.summary)
    assert "a + b" in git(tmp_path / "top", "show", f"{t.branch}:apps/mobile/calc.py")


def test_the_reviewers_problems_go_back_to_the_agent(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE, DONE], [NO, OK])
    assert t.verified and t.rounds == 2


def test_a_test_that_proves_nothing_is_sent_back(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    empty = ("write_file", {"path": "test_add.py", "content": "def test_add():\n    assert True\n"})
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, empty, DONE, GOOD_TEST, DONE], [OK, OK])
    assert t.verified and t.rounds == 2


def test_a_change_that_breaks_tests_or_weakens_them_is_never_verified(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    breaks = ("replace_in_file", {"path": "calc.py", "old": "return 1", "new": "return 2"})
    c, t = develop(monkeypatch, tmp_path, p, cmds, [breaks, DONE, DONE, DONE], [])
    assert not t.verified and "passed before" in t.problems[0] and "NOT verified" in c.report(t)
    (tmp_path / "data" / "code-tasks.json").unlink()
    weak = ("write_file", {"path": "test_calc.py", "content": "def test_one():\n    pass\n"})
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, weak, DONE, DONE, DONE], [OK] * 3)
    assert not t.verified and "lost assertions" in t.problems[0]


def test_a_bug_fix_without_a_test_for_it_is_sent_back(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, DONE, GOOD_TEST, DONE], [OK])
    assert t.verified and t.rounds == 2


def test_no_review_is_not_a_pass(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    _, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE], ["looks fine to me"] * 6)
    assert not t.verified and t.problems == ["no review could be made"]


def test_code_stays_on_the_pc_in_private_mode(tmp_path):
    p, _ = py_repo(tmp_path)
    c = Coder([{"provider": "groq", "model": "m"}], tmp_path / "data")
    c.cloud_ok = lambda: False
    with pytest.raises(PermissionError):
        c.start(p, "x", background=False)
    c.cloud_ok, c._deadline = lambda: False, float("inf")
    with pytest.raises(PermissionError):
        c._ask([], None)


def test_linked_dependencies_survive_the_cleanup(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    (p.path / "node_modules" / "lib").mkdir(parents=True)
    (p.path / "node_modules" / "lib" / "index.js").write_text("x")
    develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE], [OK])
    assert (p.path / "node_modules" / "lib" / "index.js").read_text() == "x"


def test_a_pull_request_asks_first_and_uploads_nothing_without_a_yes(tmp_path, monkeypatch):
    from jarvis.agent import coder as mod

    p, cmds = py_repo(tmp_path)
    c, t = develop(monkeypatch, tmp_path, p, cmds, [FIX, GOOD_TEST, DONE], [OK])
    assert c.open_pr(t, lambda q: True).startswith("ERROR")  # no GitHub remote
    git(p.path, "remote", "add", "origin", "https://user:tok@github.com/x/y.git")
    real, pushed, asked = mod.run_git, [], []

    def fake(path, *args, timeout=60):
        if args[0] in ("push", "ls-remote"):
            pushed.append(args[0])
            return True, ""
        return real(path, *args, timeout=timeout)
    monkeypatch.setattr(mod, "run_git", fake)
    monkeypatch.setattr(mod.shutil, "which", lambda n: "gh")
    assert "nothing was uploaded" in c.open_pr(t, lambda q: asked.append(q)) and "push" not in pushed
    assert "DRAFT" in asked[0] and t.branch in asked[0] and "tok" not in asked[0]


def test_secrets_are_never_uploaded(tmp_path, monkeypatch):
    p, cmds = py_repo(tmp_path)
    key = ("write_file", {"path": "k.py", "content": "K = 'gsk_" + "a" * 40 + "'\n"})
    c, t = develop(monkeypatch, tmp_path, p, cmds, [key, DONE, DONE, DONE], [OK] * 3)
    git(p.path, "remote", "add", "origin", "https://github.com/x/y.git")
    assert not t.verified and "secret" in c.open_pr(t, lambda q: True)


def test_missions_can_wait_for_a_fix_and_stop_it():
    from jarvis.tools.registry import ToolRegistry

    r = ToolRegistry()
    assert r.stopped() is False
    with r.asking(lambda q: False, lambda: True):
        assert r.stopped() is True
    assert r.stopped() is False
