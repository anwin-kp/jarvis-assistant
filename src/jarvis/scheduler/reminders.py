"""Reminders, timers, tasks and the daily briefing. Stored in JARVIS's SQLite DB; a small background
thread fires due reminders (they survive restarts; missed ones fire on startup)."""

import threading
from datetime import datetime, timedelta

from loguru import logger

from jarvis.memory.store import Memory
from jarvis.tools.registry import Risk, ToolRegistry

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders(
  id INTEGER PRIMARY KEY, text TEXT, due TEXT, repeat TEXT DEFAULT 'none', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, text TEXT, done INTEGER DEFAULT 0,
  created TEXT DEFAULT (datetime('now','localtime')), done_at TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""
REPEATS = ["none", "daily", "weekdays", "weekly"]


def next_due(due: datetime, repeat: str, now: datetime) -> datetime | None:
    if repeat == "none":
        return None
    step = timedelta(days=7 if repeat == "weekly" else 1)
    nxt = due + step
    while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
        nxt += timedelta(days=1) if repeat == "weekdays" else step
    return nxt


def time_left(due: datetime, now: datetime) -> str:
    """'in 4 min' / 'in 2 h 5 min' / 'in 3 days' (timers: "how long is left?")."""
    s = int((due - now).total_seconds())
    if s <= 0:
        return "due now"
    if s < 60:
        return f"in {s} s"
    m, h, d = s // 60 % 60, s // 3600 % 24, s // 86400
    if d:
        return f"in {d} day{'s' * (d != 1)}" + (f" {h} h" if h else "")
    return "in " + (f"{h} h " if h else "") + f"{m} min"


def fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")


class Reminders:
    def __init__(self, memory: Memory, on_fire, briefing_time: str | None = None, interval: float = 10,
                 briefing_until: str = "14:00", present_fn=None):
        """on_fire(kind, text): kind is 'reminder' or 'briefing'."""
        self.db, self.lock = memory.db, memory.lock
        # morning briefing "when you sit down": the first moment after briefing_time that you're at the PC
        self.briefing_until, self.present_fn = briefing_until, present_fn
        with self.lock:
            self.db.executescript(SCHEMA)
        self.on_fire = on_fire
        self.briefing_time = briefing_time
        self.interval = interval
        self._stop = threading.Event()

    # ---------------------------------------------------------------- data
    def add(self, text: str, due: datetime, repeat: str = "none") -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO reminders(text, due, repeat) VALUES (?,?,?)",
                                  (text, due.isoformat(timespec="seconds"), repeat))
            self.db.commit()
        return cur.lastrowid

    def pending(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, text, due, repeat FROM reminders WHERE active=1 ORDER BY due").fetchall()
        now = datetime.now()
        return [{"id": i, "text": t, "when": fmt(due := datetime.fromisoformat(d)), "in": time_left(due, now),
                 "repeat": r} for i, t, d, r in rows]

    def cancel(self, rid: int) -> bool:
        with self.lock:
            cur = self.db.execute("UPDATE reminders SET active=0 WHERE id=? AND active=1", (rid,))
            self.db.commit()
        return cur.rowcount > 0

    def check(self, now: datetime | None = None) -> int:
        """Fire everything due. Returns how many fired."""
        now = now or datetime.now()
        with self.lock:
            due = self.db.execute("SELECT id, text, due, repeat FROM reminders WHERE active=1 AND due<=?",
                                  (now.isoformat(timespec="seconds"),)).fetchall()
        for rid, text, d, repeat in due:
            when = datetime.fromisoformat(d)
            late = now - when > timedelta(minutes=2)
            self.on_fire("reminder", f"{text} (missed at {fmt(when)})" if late else text)
            nxt = next_due(when, repeat, now)
            with self.lock:
                if nxt:
                    self.db.execute("UPDATE reminders SET due=? WHERE id=?", (nxt.isoformat(timespec="seconds"), rid))
                else:
                    self.db.execute("UPDATE reminders SET active=0 WHERE id=?", (rid,))
                self.db.commit()
        if self.briefing_time:
            self._check_briefing(now)
        return len(due)

    def _check_briefing(self, now: datetime) -> None:
        h, m = (int(x) for x in self.briefing_time.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        uh, um = (int(x) for x in self.briefing_until.split(":"))
        until = now.replace(hour=uh, minute=um, second=0, microsecond=0)
        today = now.date().isoformat()
        # Once per day, between the target and `until`, at the first moment you're actually at the PC (so it isn't
        # spoken to an empty room, and isn't missed when you start late).
        if not (target <= now <= max(until, target + timedelta(hours=1))):
            return
        if self.present_fn and not self.present_fn():
            return
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key='last_briefing'").fetchone()
            if row and row[0] == today:
                return
            self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('last_briefing', ?)", (today,))
            self.db.commit()
        self.on_fire("briefing", "")

    # tasks
    def add_task(self, text: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO tasks(text) VALUES (?)", (text,))
            self.db.commit()
        return cur.lastrowid

    def tasks(self, include_done: bool = False) -> list[dict]:
        sql = "SELECT id, text, done FROM tasks" + ("" if include_done else " WHERE done=0") + " ORDER BY id"
        with self.lock:
            rows = self.db.execute(sql).fetchall()
        return [{"id": i, "task": t, "done": bool(d)} for i, t, d in rows]

    def complete_task(self, tid: int) -> bool:
        with self.lock:
            cur = self.db.execute("UPDATE tasks SET done=1, done_at=datetime('now','localtime') WHERE id=? AND done=0",
                                  (tid,))
            self.db.commit()
        return cur.rowcount > 0

    def delete_task(self, tid: int) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            self.db.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- thread
    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.check()
                except Exception:
                    logger.exception("Reminder check failed")
                self._stop.wait(self.interval)

        threading.Thread(target=loop, daemon=True, name="reminders").start()

    def stop(self) -> None:
        self._stop.set()


def toast(title: str, body: str) -> None:
    try:
        from win11toast import notify

        notify(title, body, app_id="JARVIS")
    except Exception:
        logger.exception("Toast failed")


def register(reg: ToolRegistry, rem: Reminders) -> None:
    @reg.tool(
        "create_reminder",
        "Create a reminder. Give EITHER in_minutes (for 'in 20 minutes') OR when_iso local date-time "
        "(e.g. 2026-11-21T09:00:00). repeat: none, daily, weekdays or weekly.",
        {
            "text": {"type": "string", "description": "What to remind about"},
            "in_minutes": {"type": "number"},
            "when_iso": {"type": "string"},
            "repeat": {"type": "string", "enum": REPEATS},
        },
        required=["text"],
        group="reminder",
    )
    def create_reminder(text: str, in_minutes: float | None = None, when_iso: str | None = None,
                        repeat: str = "none"):
        now = datetime.now()
        if in_minutes is not None:
            due = now + timedelta(minutes=float(in_minutes))
        elif when_iso:
            due = datetime.fromisoformat(when_iso.replace("Z", "")).replace(tzinfo=None)
        else:
            return "ERROR: give in_minutes or when_iso."
        if due <= now and repeat == "none":
            return f"ERROR: {fmt(due)} is in the past."
        while repeat == "weekdays" and due.weekday() >= 5:  # first weekday occurrence
            due += timedelta(days=1)
        rid = rem.add(text, due, repeat if repeat in REPEATS else "none")
        extra = "" if repeat == "none" else f", repeating {repeat}"
        return f"Reminder #{rid} set for {fmt(due)}{extra}: {text}"

    @reg.tool("list_reminders", "List pending reminders.", {}, group="reminder", read_only=True)
    def list_reminders():
        return rem.pending() or "No pending reminders."

    @reg.tool("cancel_reminder", "Cancel a reminder by id.", {"id": {"type": "integer"}}, group="reminder")
    def cancel_reminder(id: int):
        return "Cancelled." if rem.cancel(int(id)) else f"No pending reminder #{id}."

    @reg.tool("add_task", "Add an item to the to-do list.", {"task": {"type": "string"}}, group="reminder")
    def add_task(task: str):
        return f"Added task #{rem.add_task(task)}: {task}"

    @reg.tool("list_tasks", "List open to-do items.", {}, group="reminder", read_only=True)
    def list_tasks():
        return rem.tasks() or "Your to-do list is empty."

    @reg.tool("complete_task", "Mark a to-do item done by id.", {"id": {"type": "integer"}}, group="reminder")
    def complete_task(id: int):
        return "Marked done." if rem.complete_task(int(id)) else f"No open task #{id}."

    @reg.tool("delete_task", "Delete a to-do item by id.", {"id": {"type": "integer"}}, risk=Risk.CONFIRM,
              group="reminder")
    def delete_task(id: int):
        return "Deleted." if rem.delete_task(int(id)) else f"No task #{id}."
