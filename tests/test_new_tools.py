"""Browser tabs, exact maths/units/dates, network and desk tools."""

from datetime import date

import pytest

from jarvis.tools import tab_tools
from jarvis.tools.tab_tools import Tab, close_tabs

# ------------------------------------------------------------------ tabs (a fake tab strip)


class FakeStrip:
    def __init__(self, names, active=0, stuck=()):
        self.items = [Tab(n, i, i == active) for i, n in enumerate(names)]
        self.stuck, self.front, self.closed = set(stuck), True, []

    def tabs(self):
        return [Tab(t.name, t.key, t.selected) for t in self.items]

    def activate(self, key):
        for t in self.items:
            t.selected = t.key == key
        return any(t.key == key for t in self.items)

    def close_active(self):
        if not self.front:
            return False
        t = next(t for t in self.items if t.selected)
        if t.name in self.stuck:  # e.g. "Leave site?" dialog
            return True
        self.items.remove(t)
        self.closed.append(t.name)
        if self.items:
            self.items[0].selected = True
        return True


@pytest.fixture(autouse=True)
def fast_wait(monkeypatch):
    from jarvis.tools import registry

    monkeypatch.setattr(registry, "wait_for", lambda check, timeout=4.0, every=0.4: check() or None)


def test_keep_only_one_tab_closes_all_the_others():
    s = FakeStrip(["Gmail", "YouTube", "Docs", "News"], active=2)
    closed, problem = close_tabs(s, lambda tabs: [t for t in tabs if t.key != 2])
    assert problem is None and [t.name for t in s.tabs()] == ["Docs"]
    assert sorted(closed) == ["Gmail", "News", "YouTube"]


def test_never_closes_the_last_tab_or_presses_ctrl_w_elsewhere():
    s = FakeStrip(["Only"])
    closed, problem = close_tabs(s, lambda tabs: tabs)
    assert not closed and "last tab" in problem
    s = FakeStrip(["A", "B"])
    s.front = False  # the browser wouldn't come to the front
    closed, problem = close_tabs(s, lambda tabs: [tabs[1]])
    assert not closed and "Ctrl+W" in problem and len(s.tabs()) == 2


def test_a_tab_that_refuses_to_close_stops_and_says_so():
    s = FakeStrip(["A", "Form (unsaved)", "C"], stuck={"Form (unsaved)"})
    closed, problem = close_tabs(s, lambda tabs: [t for t in tabs if t.name != "A"])
    assert closed == [] and "didn't close" in problem


def test_tab_matching_by_title_or_number():
    tabs = [Tab("Inbox - Gmail", 1, False), Tab("YouTube", 2, True)]
    assert tab_tools._matches(tabs, "gmail")[0].key == 1
    assert tab_tools._matches(tabs, "2")[0].key == 2
    assert tab_tools._matches(tabs, "9") == [] and tab_tools._matches(tabs, " ") == []


def test_tab_tools_are_private():
    from jarvis.llm.cloud_brain import PRIVATE_GROUPS
    from jarvis.llm.tool_groups import pick_groups

    assert "tabs" in PRIVATE_GROUPS
    assert "tabs" in pick_groups("keep only one tab in chrome, close the others")


# ------------------------------------------------------------------ maths, units, dates


@pytest.mark.parametrize("expr,value", [("15% of 2400", 360), ("what is 12 times 7", 84), ("(3+4)^2", 49),
                                        ("10 divided by 4", 2.5), ("2,500 plus 300", 2800), ("sqrt 16", 4),
                                        ("7 x 6", 42), ("20 percent of 50", 10)])
def test_calculate(expr, value):
    from jarvis.tools.utility_tools import calculate

    assert calculate(expr)[0] == value


@pytest.mark.parametrize("expr", ["__import__('os').system('calc')", "2 ** 100000", "open('x')", "a.b", "1/0",
                                  "factorial(100000)"])
def test_calculate_refuses_anything_but_maths(expr):
    from jarvis.tools.utility_tools import calculate

    with pytest.raises((ValueError, SyntaxError, ZeroDivisionError)):
        calculate(expr)


def test_convert_units():
    from jarvis.tools.utility_tools import convert_units

    assert convert_units(5, "miles", "km") == pytest.approx(8.04672)
    assert convert_units(70, "F", "celsius") == pytest.approx(21.111, abs=1e-3)
    assert convert_units(0, "C", "kelvin") == pytest.approx(273.15)
    assert convert_units(1, "GB", "MB") == 1024
    assert convert_units(2, "cups", "ml") == pytest.approx(473.176, abs=1e-3)
    with pytest.raises(ValueError, match="mass to length"):
        convert_units(3, "kg", "km")
    with pytest.raises(ValueError, match="unit"):
        convert_units(3, "smoots", "km")


def test_date_math():
    from jarvis.tools.utility_tools import currency_code, date_math

    today = date(2026, 11, 21)
    assert date_math("2026-12-25", today=today).endswith("is 34 days (4 weeks and 6 days) from 21 November 2026.")
    assert date_math("2026-11-01", today=today).startswith("Sunday 01 November 2026 is 20 days")
    assert date_math(days=90, today=today) == "90 days after 21 November 2026 is Friday 19 February 2027."
    assert date_math("today", today=today).startswith("That's today")
    with pytest.raises(ValueError):
        date_math("next tuesday", today=today)
    assert currency_code("rupees") == "INR" and currency_code("usd") == "USD" and currency_code("sek") == "SEK"


def test_maths_requests_get_the_exact_tools():
    from jarvis.llm.tool_groups import pick_groups

    for t in ["what's 15% of 2400", "12*7", "convert 5 miles to km", "100 dollars in rupees",
              "how many days until christmas", "what does ubiquitous mean"]:
        assert "utility" in pick_groups(t), t


# ------------------------------------------------------------------ desk tools


def test_wifi_parsing_and_urls():
    from jarvis.tools.desk_tools import normalize_url, wifi_info

    out = wifi_info("    State                  : connected\n    SSID                   : HomeNet\n"
                    "    BSSID                  : aa:bb\n    Signal                 : 86%\n"
                    "    Receive rate (Mbps)    : 866.7\n")
    assert out == {"wifi_state": "connected", "wifi_name": "HomeNet", "wifi_signal": "86%",
                   "receive_rate_mbps": "866.7"}
    assert normalize_url("youtube.com") == "https://youtube.com"
    assert normalize_url("https://x.org/a?b=1&c=2") == "https://x.org/a?b=1&c=2"
    for bad in ["javascript:alert(1)", "file:///C:/x", "calc & del", "notaurl"]:
        assert normalize_url(bad) is None, bad


def test_newest_files_skips_partial_downloads(tmp_path):
    import os
    import time

    from jarvis.tools.desk_tools import newest_files

    for i, n in enumerate(["old.pdf", "new.zip", "half.crdownload", "desktop.ini"]):
        p = tmp_path / n
        p.write_text("x")
        os.utime(p, (time.time() - 100 + i * 10,) * 2)
    assert [p.name for p in newest_files(tmp_path)] == ["new.zip", "old.pdf"]


def test_tab_requests_are_instant():
    from jarvis.core.fast_intents import FastIntents
    from jarvis.tools.registry import ToolRegistry

    reg, calls = ToolRegistry(), []

    @reg.tool("close_browser_tabs", "", {"mode": {"type": "string"}, "tab": {"type": "string"},
                                          "browser": {"type": "string"}}, required=["mode"])
    def close_browser_tabs(mode, tab="", browser="chrome"):
        calls.append((mode, tab, browser))
        return ("Closed 3 tab(s). 1 tab(s) left in that window (checked). "
                "Ctrl+Shift+T in the browser reopens closed tabs.")

    @reg.tool("switch_browser_tab", "", {"tab": {"type": "string"}, "browser": {"type": "string"}}, required=["tab"])
    def switch_browser_tab(tab, browser="chrome"):
        calls.append(("switch", tab, browser))
        return "Switched."

    fast, ok = FastIntents(reg, {}), (lambda t, a: True)
    assert fast.try_handle("Jarvis, keep only one tab in Chrome", ok).startswith("Closed 3 tab(s)")
    assert fast.try_handle("close the other tabs in edge", ok) and calls[-1] == ("others", "", "edge")
    assert fast.try_handle("close the YouTube tab", ok) and calls[-1] == ("matching", "youtube", "chrome")
    assert fast.try_handle("close duplicate tabs", ok) and calls[-1][0] == "duplicates"
    assert fast.try_handle("switch to my gmail tab", ok) == "Switched to gmail."
    n = len(calls)
    assert fast.try_handle("close all tabs", ok) is None and len(calls) == n  # not a title: the AI asks


def test_window_layouts_fill_the_work_area():
    from jarvis.tools.window_tools import layout_rects

    area = (0, 0, 2560, 1504)
    assert layout_rects(area, 2) == [(0, 0, 1280, 1504), (1280, 0, 2560, 1504)]
    assert layout_rects(area, 2, "stacked") == [(0, 0, 2560, 752), (0, 752, 2560, 1504)]
    three = layout_rects(area, 3)
    assert three[0] == (0, 0, 1280, 1504) and three[2] == (1280, 752, 2560, 1504)
    assert len(layout_rects(area, 4)) == 4 and layout_rects(area, 1) == [area]


def test_time_left_for_timers():
    from datetime import datetime, timedelta

    from jarvis.scheduler.reminders import time_left

    now = datetime(2026, 11, 21, 12, 0, 0)
    assert time_left(now + timedelta(minutes=4, seconds=20), now) == "in 4 min"
    assert time_left(now + timedelta(hours=2, minutes=5), now) == "in 2 h 5 min"
    assert time_left(now + timedelta(days=3, hours=1), now) == "in 3 days 1 h"
    assert time_left(now - timedelta(minutes=1), now) == "due now"


def test_clean_old_downloads_asks_with_details_and_moves_only_old_files(tmp_path, monkeypatch):
    import os
    import time

    from jarvis.tools import desk_tools, file_tools
    from jarvis.tools.registry import ToolRegistry

    old, new = tmp_path / "old.zip", tmp_path / "new.pdf"
    for p in (old, new, tmp_path / "half.crdownload"):
        p.write_bytes(b"x" * 1000)
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    os.utime(tmp_path / "half.crdownload", (time.time() - 40 * 86400,) * 2)
    (tmp_path / "old-folder").mkdir()
    monkeypatch.setattr(file_tools, "resolve_root", lambda name: tmp_path)
    trashed = []
    monkeypatch.setattr("send2trash.send2trash", lambda p: (trashed.append(p), os.remove(p)))

    reg, asked = ToolRegistry(), []
    desk_tools.register(reg)
    reg.confirm_fn = lambda tool, args: (asked.append(args["action"]), False)[1]  # the user says no
    assert reg.get("clean_old_downloads").func(30).startswith("Not cleaned") and old.exists()
    assert "1 files" in asked[0] and "old.zip" in asked[0]
    reg.confirm_fn = lambda tool, args: True
    out = reg.get("clean_old_downloads").func(30)
    assert out.startswith("Moved 1 old download(s)") and "checked: 0 left" in out
    assert not old.exists() and new.exists() and (tmp_path / "half.crdownload").exists()
