"""Proactive alerts: JARVIS speaks up on its own about things that need attention (low battery, low disk)."""

import ctypes
import threading
import time
from datetime import datetime, timedelta

import psutil
from loguru import logger


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000


class Proactive:
    def __init__(self, on_alert, battery_percent: int = 15, disk_free_gb: float = 10, interval: float = 60,
                 cooldown_minutes: float = 30, break_minutes: float = 0, idle_fn=idle_seconds, events_fn=None,
                 meeting_minutes: float = 10, weekly_fn=None):
        self.on_alert = on_alert  # on_alert(message)
        self.battery_percent, self.disk_free_gb = battery_percent, disk_free_gb
        self.interval, self.cooldown = interval, cooldown_minutes * 60
        self.break_seconds = break_minutes * 60  # 0 = no break reminders
        self.idle_fn, self.events_fn, self.meeting_minutes = idle_fn, events_fn, meeting_minutes
        self.weekly_fn = weekly_fn  # the weekly self-check (it keeps its own schedule)
        self._work_start: float | None = None
        self._events_checked = -1e12
        self._announced: set[str] = set()
        self._last: dict[str, float] = {}
        self._stop = threading.Event()

    def _alert(self, key: str, message: str, now: float) -> None:
        if now - self._last.get(key, -1e12) >= self.cooldown:
            self._last[key] = now
            self.on_alert(message)

    def check(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        bat = psutil.sensors_battery()
        if bat and not bat.power_plugged and bat.percent <= self.battery_percent:
            self._alert("battery", f"Battery is at {bat.percent:.0f} percent. You may want to plug in.", now)
        free_gb = psutil.disk_usage("C:\\").free / 2**30
        if free_gb < self.disk_free_gb:
            self._alert("disk", f"Drive C has only {free_gb:.1f} gigabytes free.", now)
        self._check_break(now)
        self._check_meetings(now)
        if self.weekly_fn:
            try:
                self.weekly_fn()
            except Exception as e:
                logger.debug("Weekly self-check failed: {}", e)

    def _check_break(self, now: float) -> None:
        if not self.break_seconds:
            return
        if self.idle_fn() > 300:  # 5 minutes away from the keyboard counts as a break
            self._work_start = None
        elif self._work_start is None:
            self._work_start = now
        elif now - self._work_start >= self.break_seconds:
            hours = self.break_seconds / 3600
            mins = self.break_seconds // 60
            span = f"{hours:g} hour{'s' if hours != 1 else ''}" if hours >= 1 else f"{mins:.0f} minutes"
            self._last.pop("break", None)
            self._alert("break", f"You've been working for {span}. Time for a short break?", now)
            self._work_start = now

    def _check_meetings(self, now: float) -> None:
        if not self.events_fn or now - self._events_checked < 300:
            return
        self._events_checked = now
        try:
            events = self.events_fn(self.meeting_minutes + 5)
        except Exception as e:
            logger.debug("Upcoming events check failed: {}", e)
            return
        soon = datetime.now().astimezone() + timedelta(minutes=self.meeting_minutes)
        for event_id, title, start in events:
            if event_id not in self._announced and start <= soon:
                self._announced.add(event_id)
                mins = max(0, round((start - datetime.now().astimezone()).total_seconds() / 60))
                self.on_alert(f"{title} starts in {mins} minutes." if mins else f"{title} is starting now.")

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.check()
                except Exception:
                    logger.exception("Proactive check failed")

        threading.Thread(target=loop, daemon=True, name="proactive").start()

    def stop(self) -> None:
        self._stop.set()
