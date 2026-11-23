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
