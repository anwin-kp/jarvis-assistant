"""Proactive alerts: JARVIS speaks up on its own about things that need attention (low battery, low disk)."""

import threading
import time

import psutil
from loguru import logger


class Proactive:
    def __init__(self, on_alert, battery_percent: int = 15, disk_free_gb: float = 10, interval: float = 60,
                 cooldown_minutes: float = 30):
        self.on_alert = on_alert  # on_alert(message)
        self.battery_percent, self.disk_free_gb = battery_percent, disk_free_gb
        self.interval, self.cooldown = interval, cooldown_minutes * 60
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
