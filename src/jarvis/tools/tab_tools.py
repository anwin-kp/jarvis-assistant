"""Tabs in the user's own Chrome / Edge / Brave windows: list, switch, close (matching, all others, duplicates).

"Keep only one tab in Chrome" used to be a click-and-list loop through generic UI tools that ran out of steps.
Here it is one action. Each tab is closed the way a person would: select it, check (UI Automation) that it really
is the selected tab and that the browser is the foreground window, press Ctrl+W, and check the tab is gone. If any
check fails, it stops and says so. Ctrl+Shift+T in the browser brings closed tabs back.

Tab titles show what the user browses: the group is private (local model only, never sent to the cloud)."""

import re
import time
from dataclasses import dataclass

from jarvis.agent.uia import on_uia_thread
from jarvis.tools.registry import Risk, ToolRegistry

BROWSERS = {"chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
            "brave": "brave"}
HUD_TITLES = ("J.A.R.V.I.S.",)  # JARVIS's own HUD is an Edge app window: never touch it
MAX_CLOSE = 60


@dataclass
class Tab:
    name: str
    key: object  # stable identity while the tab exists (UI Automation runtime id)
    selected: bool


class Strip:
    """One browser window's tab strip, through UI Automation (call on the automation thread)."""

    def __init__(self, hwnd: int, title: str):
        from pywinauto import Desktop

        self.hwnd, self.title = hwnd, title
        self.win = Desktop(backend="uia").window(handle=hwnd)
        self._els: dict = {}

    def tabs(self) -> list[Tab]:
        out, self._els = [], {}
        for el in self.win.descendants(control_type="TabItem"):
            try:
                info = el.element_info
                name = (info.name or "").strip()
                if not name:
                    continue
                key = tuple(info.runtime_id or ()) or id(el)
                try:
                    sel = bool(el.iface_selection_item.CurrentIsSelected)
                except Exception:
                    sel = False
                self._els[key] = el
                out.append(Tab(name, key, sel))
            except Exception:
                continue
        return out

    def _front(self) -> bool:
        import win32gui

        if win32gui.GetForegroundWindow() == self.hwnd:
            return True
        try:
            self.win.set_focus()
        except Exception:
            pass
        time.sleep(0.15)
        return win32gui.GetForegroundWindow() == self.hwnd

    def activate(self, key) -> bool:
        el = self._els.get(key)
        if el is None or not self._front():
            return False
        try:
            el.iface_selection_item.Select()
        except Exception:
            try:
                el.click_input()
            except Exception:
                return False
        from jarvis.tools.registry import wait_for

        return bool(wait_for(lambda: any(t.key == key and t.selected for t in self.tabs()), timeout=1.5, every=0.1))

    def close_active(self) -> bool:
        if not self._front():  # Ctrl+W must never go to another app
            return False
        from pywinauto.keyboard import send_keys

        send_keys("^w")
        return True


def _matches(tabs: list[Tab], what: str) -> list[Tab]:
    """By number from the list ("3") or by part of the title, case-insensitive."""
    w = what.strip().lower()
    if w.isdigit():
        i = int(w) - 1
        return [tabs[i]] if 0 <= i < len(tabs) else []
    return [t for t in tabs if w and w in t.name.lower()]


def _gone(strip, key, before: int) -> bool:
    from jarvis.tools.registry import wait_for

    return bool(wait_for(lambda: (ts := strip.tabs()) is not None and len(ts) < before
                         and all(t.key != key for t in ts), timeout=2.0, every=0.1))


def close_tabs(strip, which) -> tuple[list[str], str | None]:
    """Close the tabs which(tabs) picks, one at a time, re-reading the strip after each (the tree changes).
    Returns (closed titles, problem or None)."""
    closed: list[str] = []
    done: set = set()
    for _ in range(MAX_CLOSE):
        tabs = strip.tabs()
        targets = [t for t in which(tabs) if t.key not in done]
        if not targets:
            return closed, None
        if len(tabs) <= 1:
            return closed, "that's the last tab in the window, so I left it (closing it would close the window)"
        t = targets[0]
        done.add(t.key)
        if not strip.activate(t.key):
            return closed, f"I couldn't select the tab '{t.name}' (the browser may be busy or minimised)"
        if not strip.close_active():
            return closed, "the browser window wouldn't come to the front, so I didn't press Ctrl+W"
        if not _gone(strip, t.key, len(tabs)):
            return closed, f"the tab '{t.name}' didn't close (it may be asking something, e.g. 'Leave site?')"
        closed.append(t.name)
    return closed, f"stopped after {MAX_CLOSE} tabs"


def short(title: str) -> str:
    return re.sub(r"\s+", " ", title)[:70]


def _summary(closed: list[str], problem: str | None, left: int) -> str:
    if not closed and not problem:
        return "No tabs matched, so nothing was closed."
    names = ", ".join(f"'{short(n)}'" for n in closed[:5]) + (f" and {len(closed) - 5} more" if len(closed) > 5 else "")
    text = f"Closed {len(closed)} tab(s){': ' + names if closed else ''}. {left} tab(s) left in that window (checked)."
    if problem:
        text = ("WARNING: " if closed else "ERROR: ") + text + f" Stopped because {problem}."
    return text + (" Ctrl+Shift+T in the browser reopens closed tabs." if closed else "")


def register(reg: ToolRegistry) -> None:
    def strips(browser: str) -> list[Strip]:
        from jarvis.tools.window_tools import list_windows

        exe = BROWSERS.get((browser or "chrome").lower().strip(), (browser or "chrome").lower().strip())
        wins = [w for w in list_windows() if w["app"].lower() == exe and not w["title"].startswith(HUD_TITLES)]
        out = [Strip(w["hwnd"], w["title"]) for w in wins]  # front-most window first
        return [s for s in out if s.tabs()]

    def first(browser: str) -> Strip:
        found = strips(browser)
        if not found:
            raise LookupError(f"no {browser or 'chrome'} window with tabs is open")
        return found[0]

    @reg.tool("list_browser_tabs", "List the open tabs in the user's own browser windows (Chrome by default; "
              "edge, brave). The active tab is marked with *.",
              {"browser": {"type": "string", "description": "chrome (default), edge or brave"}},
              required=[], group="tabs", read_only=True)
    @on_uia_thread
    def list_browser_tabs(browser: str = "chrome"):
        found = strips(browser)
        if not found:
            return f"No {browser or 'chrome'} window with tabs is open."
        lines = []
        for n, s in enumerate(found, 1):
            tabs = s.tabs()
            lines.append(f"Window {n} ({len(tabs)} tabs):")
            lines += [f"  [{i}]{' *' if t.selected else ''} {short(t.name)}" for i, t in enumerate(tabs, 1)]
        return "\n".join(lines)

    @reg.tool("switch_browser_tab", "Bring a tab to the front in the user's browser, by part of its title or its "
              "number from list_browser_tabs.",
              {"tab": {"type": "string"}, "browser": {"type": "string"}}, required=["tab"], group="tabs")
    @on_uia_thread
    def switch_browser_tab(tab: str, browser: str = "chrome"):
        for s in strips(browser):
            hits = _matches(s.tabs(), tab)
            if hits:
                if s.activate(hits[0].key):
                    return f"Switched to the tab '{short(hits[0].name)}' (checked: it's the active tab)."
                return f"ERROR: found '{short(hits[0].name)}' but couldn't bring it to the front."
        return f"ERROR: no open tab matches '{tab}'. Use list_browser_tabs to see the tabs."

    @reg.tool("close_browser_tabs", "Close tabs in the user's browser. mode: 'matching' = tabs whose title contains "
              "`tab` (or its number); 'others' = every tab except one (keep `tab`, or the active tab when empty: "
              "'keep only this tab'); 'duplicates' = extra copies of the same page. Checks each tab really closed.",
              {"mode": {"type": "string", "enum": ["matching", "others", "duplicates"]},
               "tab": {"type": "string", "description": "part of a title, or a number from list_browser_tabs"},
               "browser": {"type": "string"}},
              required=["mode"], risk=Risk.CONFIRM, group="tabs")
    @on_uia_thread
    def close_browser_tabs(mode: str, tab: str = "", browser: str = "chrome"):
        if mode == "matching":
            if not tab.strip():
                return "ERROR: say which tab to close (part of its title or its number)."
            s = next((s for s in strips(browser) if _matches(s.tabs(), tab)), None)
            if s is None:
                return f"ERROR: no open tab matches '{tab}', so nothing was closed."
            matched = {t.key for t in _matches(s.tabs(), tab)}  # fixed up front: numbers shift as tabs close
            closed, problem = close_tabs(s, lambda tabs: [t for t in tabs if t.key in matched])
        elif mode == "others":
            s = first(browser)
            tabs = s.tabs()
            keep = _matches(tabs, tab) if tab.strip() else [t for t in tabs if t.selected][:1]
            if not keep:
                return (f"ERROR: no tab matches '{tab}'" if tab.strip() else "ERROR: I can't tell which tab is "
                        "active") + ", so nothing was closed. Say which tab to keep."
            kept = keep[0].key
            closed, problem = close_tabs(s, lambda tabs: [t for t in tabs if t.key != kept])
            if not problem:
                s.activate(kept)
        elif mode == "duplicates":
            s = first(browser)

            def extra(tabs):
                seen, out = set(), []
                for t in tabs:
                    (out.append(t) if t.name in seen else seen.add(t.name))
                return out

            closed, problem = close_tabs(s, extra)
        else:
            return "ERROR: mode must be matching, others or duplicates."
        text = _summary(closed, problem, len(s.tabs()))
        more = len(strips(browser)) - 1
        if mode == "others" and more > 0 and not problem:
            text += f" ({more} other {browser or 'chrome'} window(s) are still open, untouched.)"
        return text
