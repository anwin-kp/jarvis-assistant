"""Context ('this' = the active window, private) and routine suggestions (never acted on)."""

import json

from jarvis.core.routines import find_routines


def row(day, hm, tool="open_app", args=None, status="ok"):
    return (f"2026-11-{day:02d} {hm}:00", tool, json.dumps(args if args is not None else {"name": "spotify"}), status)


def test_the_same_thing_at_the_same_time_on_three_days_is_a_routine():
    rows = [row(16, "09:05"), row(17, "09:20"), row(18, "08:55"), row(18, "21:00"),  # Mon-Wed mornings
            row(19, "14:00", "get_weather", {}), row(20, "18:00", "get_weather", {})]  # 2 days, different times
    (r,) = find_routines(rows)
    assert r["tool"] == "open_app" and r["what"] == "spotify" and r["days"] == 3 and r["weekdays"]
    assert r["text"] == "open Spotify around 9:05 on weekdays (3 days)"


def test_test_bursts_failures_and_scattered_times_are_not_routines():
    burst = [row(21, f"00:{m:02d}", args={"name": "notepad"}) for m in range(0, 40, 5)]  # 8 opens, one day
    failed = [row(d, "10:00", status="error") for d in (16, 17, 18)]
    scattered = [row(16, "07:00", args={"name": "chrome"}), row(17, "13:00", args={"name": "chrome"}),
                 row(18, "22:00", args={"name": "chrome"})]
    assert find_routines(burst + failed + scattered) == []


def test_window_context_only_when_the_words_point_at_it_and_only_in_the_private_block():
    from jarvis.core.orchestrator import PERSONAL_END, PERSONAL_START, Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.location_fn, o.interview, o.persona = None, False, "PERSONA"
    o.window_fn = lambda: "Code: app.py - Visual Studio Code"
    o.memory = type("M", (), {"profile": lambda self, q: "", "recent_summaries": lambda self: []})()
    o.brain = type("Router", (), {"cloud": object()})()
    p = o.system_prompt("what's this error?")
    assert "Code: app.py" in p and p.index(PERSONAL_START) < p.index("Code: app.py") < p.index(PERSONAL_END)
    assert "Code: app.py" not in o.system_prompt("what's the weather in Kochi?")
