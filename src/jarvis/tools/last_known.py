"""Last known answers for internet tools (weather, location, news, unread emails, calendar).

When the internet is down, a tool that needs it answers with its last good result and says how old it is,
instead of failing. Only when the internet is really unreachable: a genuine error ("no place called X") is
never covered up with old data. Email and calendar results stay in memory only (never written to disk)."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

MAX_AGE_HOURS = 24  # older than this isn't worth repeating
HERE = {"", "here", "my location", "current location", "near me", "local"}


def internet_up(timeout: float = 1.5) -> bool:
    """Can we reach the internet at all? (TCP to two public DNS servers; no lookup needed.)"""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    h, m = divmod(m, 60)
    return f"{h} h {m} min ago" if h < 6 and m else f"{h} h ago"


def _key(name: str, args: dict) -> str:
    # "today"/"tomorrow" mean a different day tomorrow: key them by the real date
    # {}, {"city": ""} and {"city": "here"} are the same question: leave out empty / "where I am" values
    fixed = {}
    for k, v in sorted(args.items()):
        if isinstance(v, str):
            v = v.strip().lower()
            if v in HERE:
                continue
            if v in ("today", "tomorrow"):
                v = str(date.today() + timedelta(days=v == "tomorrow"))
        if v is None:
            continue
        fixed[k] = v
    return f"{name}:{json.dumps(fixed, ensure_ascii=False, sort_keys=True)}"


class LastKnown:
    def __init__(self, path: Path | None = None, max_age_hours: float = MAX_AGE_HOURS):
        self.path, self.max_age = path, max_age_hours * 3600
        self._disk: dict[str, dict] = {}
        self._ram: dict[str, dict] = {}
        self._lock = threading.Lock()
        if path:
            try:
                self._disk = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._disk = {}

    def put(self, name: str, args: dict, text: str, where: str) -> None:
        entry = {"at": time.time(), "text": text}
        with self._lock:
            if where == "disk":
                self._disk[_key(name, args)] = entry
                self._save()
            else:
                self._ram[_key(name, args)] = entry

    def get(self, name: str, args: dict) -> str | None:
        k = _key(name, args)
        e = self._ram.get(k) or self._disk.get(k)
        if not e or time.time() - e["at"] > self.max_age:
            return None
        age = _age(time.time() - e["at"])
        return (f"OFFLINE: no internet connection, so this is the last known result, from {age}. Begin your answer "
                f"with \"I'm offline, but as of {age}\" and then give it.\n{e['text']}")

    def _save(self) -> None:
        if not self.path:
            return
        now = time.time()
        self._disk = {k: v for k, v in self._disk.items() if now - v["at"] < self.max_age}
        try:
            self.path.write_text(json.dumps(self._disk, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.debug("Couldn't save last known answers: {}", e)
