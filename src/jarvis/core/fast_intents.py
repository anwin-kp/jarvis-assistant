"""Instant commands: the most common requests are recognised directly (no AI round-trip, ~0.1 s).
They still go through the tool registry, so permissions, the audit log and the HUD activity feed apply."""

import re
from datetime import datetime

_PREFIX = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis[\s,]+)?(?:please\s+|can you\s+|could you\s+|would you\s+)*")
_SUFFIX = re.compile(r"(?:[\s,]+(?:please|now|jarvis|for me))+$")


def normalize(text: str) -> str:
    t = text.lower().strip().strip(".!?, ")
    t = _PREFIX.sub("", t)
    t = _SUFFIX.sub("", t)
    return re.sub(r"\s+", " ", t).strip(".!?, ")


class FastIntents:
    def __init__(self, reg, cfg: dict):
        self.reg = reg
        self.apps = {k.lower() for k in (cfg.get("security", {}).get("app_allowlist") or {})}
        r = re.compile
        self.rules = [
            (r(r"(?:pause|stop)(?: the)?(?: music| song| video| playback| media| it)?"), self._media("play_pause", "Paused.")),
            (r(r"(?:resume|play|unpause)(?: the)? ?(?:music|song|playback|it)?"), self._media("play_pause", "Playing.")),
            (r(r"(?:next|skip)(?: the)?(?: song| track| video| this song| this track| this)?|play the next (?:song|track)"),
             self._media("next", "Skipping.")),
            (r(r"(?:previous|last|go back)(?: the)?(?: song| track)?|play the (?:previous|last) (?:song|track)"),
             self._media("previous", "Going back.")),
            (r(r"(?:set |change |turn |put )?(?:the )?volume (?:to |at )?(\d{1,3})(?: ?percent|%)?"), self._volume),
            (r(r"(?:turn (?:the )?volume |volume |turn it )(up|down)"), self._volume_step),
            (r(r"(mute|unmute)(?: the)?(?: sound| audio| volume| speakers| computer)?"), self._mute),
            (r(r"(?:set |change |turn |put )?(?:the )?(?:screen )?brightness (?:to |at )?(\d{1,3})(?: ?percent|%)?"),
             self._brightness),
            (r(r"what(?:'s| is) the time|what time is it|tell me the time|time please|the time"), self._time),
            (r(r"what(?:'s| is) (?:the date|today'?s date)|what day is (?:it|today)(?: today)?|what's today"), self._date),
            (r(r"lock (?:the |my )?(?:screen|pc|computer|laptop)"), self._tool("lock_screen", {}, "Locking.")),
            (r(r"(?:turn on |enable |switch to |go )?(dark|light) mode(?: on)?"), self._dark),
            (r(r"(?:turn off |disable )dark mode"), lambda m, c: self._run("set_dark_mode", {"on": False}, c, "Light mode on.")),
            (r(r"(?:take a )?screen ?shot|capture (?:the|my) screen"), self._tool("take_screenshot", {}, None)),
            (r(r"show (?:me )?(?:the )?desktop|minimi[sz]e (?:everything|all(?: windows)?|all the windows)"),
             self._tool("show_desktop", {}, "Done.")),
            (r(r"(?:turn on |enable |start |activate )gaming mode|gaming mode on"),
             self._tool("set_gaming_mode", {"on": True}, None)),
            (r(r"(?:turn off |disable |stop |exit )gaming mode|gaming mode off"),
             self._tool("set_gaming_mode", {"on": False}, None)),
            (r(r"(?:open|launch|start|run) (?:the )?(.+?)(?: app| application)?"), self._open),
            (r(r"(?:never ?mind,? )?(?:cancel|abort|stop) (?:the )?(?:shutdown|shut down|restart)|(?:never ?mind,? )?(?:don'?t|do not) (?:shut ?down|restart)(?: the (?:pc|computer|laptop))?"),
             self._tool("cancel_shutdown", {}, None)),
            (r(r"(?:switch|go|change) to (?:the )?(.+?)(?: window)?|(?:bring up|focus(?: on)?) (?:the )?(.+?)(?: window)?"), self._switch),
        ]

    # ---------------------------------------------------------------- public
    def try_handle(self, text: str, confirm) -> str | None:
        """Returns the spoken reply if the request was handled instantly, else None (send it to the AI)."""
        t = normalize(text)
        if not t or len(t) > 60:
            return None
        for pattern, handler in self.rules:
            m = pattern.fullmatch(t)
            if m:
                reply = handler(m, confirm)
                if reply is not None:
                    return reply
        return None

    # ---------------------------------------------------------------- helpers
    def _run(self, name: str, args: dict, confirm, ok: str | None) -> str | None:
        if self.reg.get(name) is None:
            return None
        r = self.reg.execute(name, args, confirm)
        if r.error:
            return r.text.replace("ERROR: ", "")
        return ok or r.text

    def _tool(self, name, args, ok):
        return lambda m, confirm: self._run(name, args, confirm, ok)

    def _media(self, action, ok):
        return lambda m, confirm: self._run("media_control", {"action": action}, confirm, ok)

    def _volume(self, m, confirm):
        pct = max(0, min(100, int(m.group(1))))
        return self._run("set_volume", {"percent": pct}, confirm, f"Volume {pct} percent.")

    def _volume_step(self, m, confirm):
        cur = self.reg.execute("get_volume", {}, confirm)
        try:
            now = int(__import__("json").loads(cur.text)["percent"])
        except Exception:
            return None
        pct = max(0, min(100, now + (10 if m.group(1) == "up" else -10)))
        return self._run("set_volume", {"percent": pct}, confirm, f"Volume {pct} percent.")

    def _mute(self, m, confirm):
        on = m.group(1) == "mute"
        return self._run("set_mute", {"muted": on}, confirm, "Muted." if on else "Unmuted.")

    def _brightness(self, m, confirm):
        pct = max(0, min(100, int(m.group(1))))
        return self._run("set_brightness", {"percent": pct}, confirm, f"Brightness {pct} percent.")

    def _time(self, m, confirm):
        return datetime.now().strftime("It's %I:%M %p.").replace(" 0", " ")

    def _date(self, m, confirm):
        return datetime.now().strftime("Today is %A, %d %B %Y.").replace(" 0", " ")

    def _dark(self, m, confirm):
        dark = m.group(1) == "dark"
        return self._run("set_dark_mode", {"on": dark}, confirm, f"{'Dark' if dark else 'Light'} mode on.")

    def _switch(self, m, confirm):
        name = (m.group(1) or m.group(2) or "").strip()
        if not name or self.reg.get("window_action") is None:
            return None
        r = self.reg.execute("window_action", {"name": name, "action": "focus"}, confirm)
        return None if r.error else f"Switched to {name}."  # no such window: let the AI decide (maybe open it)

    def _open(self, m, confirm):
        name = m.group(1).strip()
        if name not in self.apps:
            return None  # unknown app: let the AI handle it (it may be a file, website, ...)
        return self._run("open_app", {"name": name}, confirm, f"Opening {name}.")
