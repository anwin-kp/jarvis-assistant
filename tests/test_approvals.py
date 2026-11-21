from jarvis.tools.registry import Risk, ToolRegistry


def make():
    reg = ToolRegistry()
    ran = []
    for name, risk in (("press_keys", Risk.CONFIRM), ("move_file", Risk.CONFIRM), ("delete_file", Risk.DANGER)):
        reg.tool(name, name, {}, risk=risk)(lambda name=name: ran.append(name) or "done")
    reg.no_approval = {"press_keys", "delete_file"}  # delete_file listed by mistake: must still ask
    return reg, ran


def test_listed_tools_run_without_asking_and_danger_always_asks():
    reg, ran = make()
    asked = []
    confirm = lambda tool, args: asked.append(tool.name) or True  # noqa: E731
    for name in ("press_keys", "move_file", "delete_file"):
        reg.execute(name, {}, confirm)
    assert asked == ["move_file", "delete_file"] and ran == ["press_keys", "move_file", "delete_file"]


def test_risky_step_asks_and_refuses_without_a_prompt():
    reg, _ = make()
    assert reg.ask("click 'Buy now'") is False  # no approval prompt available
    seen = []
    reg.confirm_fn = lambda tool, args: seen.append(args["action"]) or True
    assert reg.ask("click 'Buy now'") is True and seen == ["click 'Buy now'"]


def test_wait_for_polls_until_true_or_timeout():
    from jarvis.tools.registry import wait_for

    n = []
    assert wait_for(lambda: n.append(1) or (len(n) >= 3 and "ok"), timeout=2, every=0.01) == "ok"
    assert wait_for(lambda: None, timeout=0.05, every=0.01) is None
    assert wait_for(lambda: 1 / 0, timeout=0.05, every=0.01) is None  # errors count as "not yet"
