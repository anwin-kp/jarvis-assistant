from jarvis.tools.registry import Risk, ToolRegistry

ALLOW = lambda t, a: True
DENY = lambda t, a: False


def make():
    reg, ran = ToolRegistry(), []

    @reg.tool("echo", "Echo text.", {"msg": {"type": "string"}})
    def echo(msg):
        return msg

    @reg.tool("nuke", "Dangerous.", {}, risk=Risk.DANGER)
    def nuke():
        ran.append("nuke")
        return "boom"

    @reg.tool("tweak", "Needs confirm.", {}, risk=Risk.CONFIRM)
    def tweak():
        ran.append("tweak")
        return "done"

    @reg.tool("broken", "Raises.", {})
    def broken():
        raise RuntimeError("kaput")

    return reg, ran


def test_safe_tool_runs_without_confirm():
    reg, _ = make()
    asked = []
    r = reg.execute("echo", {"msg": "hi"}, confirm=lambda t, a: asked.append(1) or False)
    assert r.text == "hi" and not r.error and not asked


def test_danger_tool_blocked_when_declined():
    reg, ran = make()
    r = reg.execute("nuke", {}, confirm=DENY)
    assert r.error and "declined" in r.text and ran == []


def test_confirm_tool_runs_when_approved():
    reg, ran = make()
    r = reg.execute("tweak", {}, confirm=ALLOW)
    assert r.text == "done" and ran == ["tweak"]


def test_dry_run_does_not_execute():
    reg, ran = make()
    reg.dry_run = True
    r = reg.execute("nuke", {}, confirm=ALLOW)
    assert "dry run" in r.text and ran == []


def test_dry_run_blocks_safe_tools_that_change_state():
    reg, changed = ToolRegistry(dry_run=True), []

    @reg.tool("set_thing", "Safe but changes state.", {})
    def set_thing():
        changed.append(1)

    @reg.tool("read_thing", "Read-only.", {}, read_only=True)
    def read_thing():
        return "value"

    assert "dry run" in reg.execute("set_thing", {}, ALLOW).text and changed == []
    assert reg.execute("read_thing", {}, ALLOW).text == "value"


def test_invented_args_dropped_and_json_string_args():
    reg, _ = make()
    assert reg.execute("echo", {"msg": "a", "bogus": 1}, ALLOW).text == "a"
    assert reg.execute("echo", '{"msg": "b"}', ALLOW).text == "b"


def test_missing_args_and_unknown_tool():
    reg, _ = make()
    assert reg.execute("echo", {}, ALLOW).error
    assert reg.execute("nope", {}, ALLOW).error


def test_exceptions_become_error_results():
    reg, _ = make()
    r = reg.execute("broken", {}, ALLOW)
    assert r.error and "kaput" in r.text


def test_schemas_filtered_by_group():
    reg, _ = make()
    assert {s["function"]["name"] for s in reg.schemas({"core"})} == {"echo", "nuke", "tweak", "broken"}
    assert reg.schemas({"web"}) == []
