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


_IN = r"(?: (?:in|on) (?:the )?(chrome|edge|brave)(?: browser)?)?"


def _sound(s: str) -> str:
    """Spelling folded toward sound: "not padder" -> "notpader", "spotty fly" -> "spotyfly"."""
    s = re.sub(r"[^a-z]", "", s.lower()).replace("ph", "f").replace("ck", "k")
    s = s[:1] + s[1:].replace("y", "i")  # "fly" ~ "fi"
    return re.sub(r"(.)\1+", r"\1", s)


def heard_app(said: str, apps) -> str | None:
    """The known app a (mis)heard name means: exact, else the one clear sound-alike ("not padder" -> notepad).
    Strict on purpose: a wrong guess would open or close the wrong app, so near ties go to the AI instead."""
    import difflib

    said = said.lower().strip()
    if said in apps:
        return said
    s = _sound(said)
    if len(s) < 4:
        return None
    scored = sorted(((difflib.SequenceMatcher(None, s, _sound(a)).ratio(), a) for a in apps), reverse=True)
    if not scored or scored[0][0] < 0.8 or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1):
        return None
    return scored[0][1]


class FastIntents:
    def __init__(self, reg, cfg: dict):
        self.reg = reg
        self.apps = {k.lower() for k in (cfg.get("security", {}).get("app_allowlist") or {})}
        r = re.compile
        self.rules = [
            (r(r"(?:pause|stop)(?: the)?(?: music| song| video| playback| media| it)?"), self._media("pause", None)),
            (r(r"(?:resume|play|unpause|continue|start playing|keep playing)(?: the)? ?(?:music|song|playback|it)?"),
             self._media("play", None)),
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
            # browser tabs (before open/close: "close the YouTube tab" is a tab, not an app)
            (r(rf"(?:keep|leave) only (?:this|the current|the active|one|1) tab(?: open)?{_IN}"
               rf"|close (?:all )?(?:the )?other tabs{_IN}"), self._tabs("others")),
            (r(rf"close (?:all )?(?:the )?duplicate tabs{_IN}"), self._tabs("duplicates")),
            (r(rf"close (?:the |my )?(.+?) tabs?{_IN}"), self._tabs("matching")),
            (r(rf"(?:switch|go|change) to (?:the |my )?(.+?) tab{_IN}"), self._tab_switch),
            (r(r"(?:open|launch|start|run) (?:the )?(.+?)(?: app| application)?"), self._open),
            # "closed"/"clothes": how "close" is often transcribed
            (r(r"(?:close|closed|clothes|quit|exit|kill) (?:the )?(.+?)(?: app| application)?"), self._close),
            (r(r"(?:never ?mind,? )?(?:cancel|abort|stop) (?:the )?(?:shutdown|shut down|restart)|(?:never ?mind,? )?(?:don'?t|do not) (?:shut ?down|restart)(?: the (?:pc|computer|laptop))?"),
             self._tool("cancel_shutdown", {}, None)),
            # "mark all of them as read" (after a list), "mark my emails as read": every unread email, checked
            (r(r"mark (?:all (?:of )?)?(?:them|those|these|everything|all|(?:all )?(?:of )?(?:my |the )?(?:unread )?"
               r"(?:e-?mails?|mails?|messages?)(?: in (?:my|the) inbox)?) (?:as )?(?:read|red)"),
             self._tool("mark_emails_read", {"ids": "all", "read": True}, None)),
            (r(r"(?:switch|go|change) to (?:the )?(.+?)(?: window)?|(?:bring up|focus(?: on)?) (?:the )?(.+?)(?: window)?"), self._switch),
        ]

    # ---------------------------------------------------------------- public
    def matches(self, text: str) -> bool:
        """Would this be an instant command? (no side effects)"""
        t = normalize(text)
        return bool(t) and len(t) <= 60 and any(p.fullmatch(t) for p, _ in self.rules)

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

    def _tabs(self, mode):
        def handle(m, confirm):
            if self.reg.get("close_browser_tabs") is None:
                return None
            groups = [g for g in m.groups()]
            browser = groups[-1] or "chrome"
            tab = groups[0] if mode == "matching" else ""
            if mode == "matching" and tab.lower() in ("other", "all", "all the", "all other", "this", "these"):
                return None  # "close all tabs": not a title; let the AI ask
            r = self.reg.execute("close_browser_tabs", {"mode": mode, "tab": tab, "browser": browser}, confirm)
            text = r.text.removeprefix("WARNING: ")
            if r.error and "matches" in text:
                return None  # no tab by that name: maybe it's a window or an app, the AI decides
            return text.removeprefix("ERROR: ").replace(" Ctrl+Shift+T in the browser reopens closed tabs.", "")
        return handle

    def _tab_switch(self, m, confirm):
        if self.reg.get("switch_browser_tab") is None:
            return None
        r = self.reg.execute("switch_browser_tab", {"tab": m.group(1), "browser": m.group(2) or "chrome"}, confirm)
        return None if r.error else f"Switched to {m.group(1)}."

    def _close(self, m, confirm):
        name = heard_app(m.group(1), self.apps)
        if not name:
            return None  # "close this tab", "close the window": the AI decides
        r = self.reg.execute("close_app", {"name": name}, confirm) if self.reg.get("close_app") else None
        if r is None:
            return None
        text = r.text.removeprefix("ERROR: ").removeprefix("WARNING: ")
        return f"Closed {name}." if text.startswith("Closed") else text.replace(" Tell the user to check it.", "")

    def _open(self, m, confirm):
        name = heard_app(m.group(1), self.apps)
        if not name:
            return None  # unknown app: let the AI handle it (it may be a file, website, ...)
        return self._run("open_app", {"name": name}, confirm, f"Opening {name}.")
