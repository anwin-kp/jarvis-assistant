from datetime import datetime, timedelta

from jarvis.memory.store import Memory
from jarvis.scheduler.reminders import Reminders, next_due, register
from jarvis.tools.registry import ToolRegistry

OK = lambda t, a: True


def make(tmp_path, briefing=None):
    fired = []
    rem = Reminders(Memory(tmp_path / "m.db"), lambda k, t: fired.append((k, t)), briefing_time=briefing)
    return rem, fired


def test_next_due():
    fri = datetime(2026, 11, 20, 9, 0)  # Friday
    assert next_due(fri, "none", fri) is None
    assert next_due(fri, "daily", fri) == datetime(2026, 11, 21, 9, 0)
    assert next_due(fri, "weekdays", fri) == datetime(2026, 11, 23, 9, 0)  # Monday
    assert next_due(fri, "weekly", fri + timedelta(days=8)) == datetime(2026, 12, 4, 9, 0)


def test_one_time_and_repeating_reminders(tmp_path):
    rem, fired = make(tmp_path)
    now = datetime(2026, 11, 20, 9, 0)
    rem.add("stretch", now - timedelta(minutes=1))
    rem.add("standup", now - timedelta(hours=1), "daily")
    assert rem.check(now) == 2
    assert fired[0] == ("reminder", "stretch")
    assert "missed at" in fired[1][1]
    assert [r["text"] for r in rem.pending()] == ["standup"]  # one-time gone, daily rescheduled
    assert rem.check(now) == 0


def test_tools_and_tasks(tmp_path):
    rem, _ = make(tmp_path)
    reg = ToolRegistry()
    register(reg, rem)
    r = reg.execute("create_reminder", {"text": "tea", "in_minutes": 5}, OK)
    assert not r.error and "Reminder #1" in r.text
    assert reg.execute("create_reminder", {"text": "x", "when_iso": "2000-01-01T00:00:00"}, OK).text.startswith("ERROR")
    assert reg.execute("cancel_reminder", {"id": 1}, OK).text == "Cancelled."
    reg.execute("add_task", {"task": "buy milk"}, OK)
    assert "buy milk" in reg.execute("list_tasks", {}, OK).text
    assert reg.execute("complete_task", {"id": 1}, OK).text == "Marked done."
    assert reg.execute("list_tasks", {}, OK).text == "Your to-do list is empty."


def test_briefing_fires_once_per_day(tmp_path):
    rem, fired = make(tmp_path, briefing="08:30")
    rem.check(datetime(2026, 11, 20, 8, 0))
    assert fired == []
    rem.check(datetime(2026, 11, 20, 8, 45))
    rem.check(datetime(2026, 11, 20, 9, 0))
    assert fired == [("briefing", "")]
