from jarvis.core.commands import KILL_SCRIPT, is_kill_command


def test_kill_phrases_match():
    for t in ["jarvis shut down", "Jarvis, shut down.", "Hey Jarvis shutdown", "jarvis shut-down now",
              "shutdown jarvis", "Shut down Jarvis!", "please shut down the jarvis", "Jarvis, please shut down"]:
        assert is_kill_command(t), t


def test_other_shutdowns_do_not_match():
    for t in ["jarvis shut down the computer", "shut down my pc", "shutdown", "how do I shut down jarvis?",
              "jarvis, what time is it", "restart jarvis"]:
        assert not is_kill_command(t), t


def test_kill_script_exists():
    assert KILL_SCRIPT.exists()


def test_bare_shutdown_only_counts_right_after_wake_word():
    assert is_kill_command("Shut down.", after_wake=True)
    assert is_kill_command("shutdown now", after_wake=True)
    assert not is_kill_command("Shut down.")
    assert not is_kill_command("shut down the computer", after_wake=True)
