"""Coding A+B: projects, safe reading (no secrets, no escaping), allowlisted dev commands, result summaries."""

import subprocess

import pytest

from jarvis.tools import code_tools
from jarvis.tools.code_tools import SECRET, Project, dev_commands, discover, summarize_output
from jarvis.tools.registry import ToolRegistry


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "Dev"
    app = root / "my_app"
    (app / "lib").mkdir(parents=True)
    (app / "pubspec.yaml").write_text("name: my_app\n")
    (app / "lib" / "main.dart").write_text("void main() {\n  print('hello');\n}\n")
    (app / ".env").write_text("API_KEY=secret\n")
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)
    reg = ToolRegistry()
    code_tools.register(reg, {"coding": {"search_roots": [str(root)]}})
    return reg, app


def run(reg, name, **args):
    return reg.execute(name, args, lambda t, a: True)


def test_projects_are_found_and_matched_loosely(repo):
    reg, app = repo
    assert "my_app (flutter)" in run(reg, "list_projects").text
    assert reg.code_project("my app").path == app and reg.code_project("my_ap").name == "my_app"


def test_reading_and_searching_stay_inside_the_project_and_skip_secrets(repo):
    reg, _ = repo
    assert "2:   print('hello');" in run(reg, "read_code_file", project="my_app", path="lib/main.dart").text
    assert run(reg, "read_code_file", project="my_app", path="../../outside.txt").error
    secret = run(reg, "read_code_file", project="my_app", path=".env")
    assert secret.error and "secret" in secret.text
    assert "main.dart:2" in run(reg, "search_code", project="my_app", query="hello").text
    assert ".env" not in run(reg, "list_code_files", project="my_app").text


def test_only_allowlisted_commands_run(repo):
    reg, _ = repo
    out = run(reg, "run_dev_command", project="my_app", command="del /s *").text
    assert out.startswith("ERROR") and "Allowed: test, analyze" in out
    assert set(dev_commands(Project("x", repo[1], ["flutter"]))) >= {"test", "analyze", "build apk"}


def test_status_keeps_gits_leading_space(repo):
    reg, app = repo
    (app / "lib" / "main.dart").write_text("void main() {}\n")
    assert "\n M lib/main.dart" in run(reg, "project_status", project="my_app").text


def test_results_are_summarized():
    assert "1 failed, 2 passed in 0.12s" in summarize_output("test", 1, "....F\n1 failed, 2 passed in 0.12s\n")
    assert "No issues found!" in summarize_output("analyze", 0, "Analyzing...\nNo issues found! (ran in 3.1s)")
    s = summarize_output("test", 1, "00:02 +3 -1: Some tests failed.\nExpected: 2 Actual: 3\nerror: boom")
    assert "FAILED (exit 1)" in s and "Some tests failed." in s and "error: boom" in s
    for f in [".env", "app/.env.local", "android/key.properties", "upload.jks", "token.json", "id_rsa"]:
        assert SECRET.search(f), f
    assert not SECRET.search("lib/main.dart")


def test_discovery_skips_dependency_folders(tmp_path):
    (tmp_path / "a" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "a" / "node_modules" / "pkg" / "package.json").write_text("{}")
    (tmp_path / "a" / "package.json").write_text("{}")
    assert list(discover([tmp_path])) == ["a"]
