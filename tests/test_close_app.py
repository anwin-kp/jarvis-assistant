"""close_app finds the app by its real process/window, checks it closed, and never claims a close it didn't do."""
import types

import psutil

from jarvis.llm.ollama_brain import unbacked_claim
from jarvis.tools import app_tools


def test_real_process_names():
    assert "calculatorapp" in app_tools.app_names("calculator", "calc.exe")
    assert "spotify" in app_tools.app_names("spotify", "spotify:")
    assert app_tools.app_names("notepad", "notepad.exe") == {"notepad"}
    assert "explorer" not in app_tools.app_names("explorer", "explorer.exe")


def test_store_app_window_matches_by_title():
    w = {"hwnd": 1, "title": "Calculator", "app": "ApplicationFrameHost"}
    assert app_tools._is_app_window(w, "calculator", {"calculator", "calculatorapp"})
    assert not app_tools._is_app_window({**w, "app": "chrome", "title": "calculator - Google"}, "calculator",
                                        {"calculator", "calculatorapp"})


def test_not_running_is_an_error(monkeypatch):
    monkeypatch.setattr("jarvis.tools.window_tools.list_windows", lambda: [])
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([]))
    out = app_tools.close_app_named("spotify", {"spotify": "spotify:"})
    assert out.startswith("ERROR") and "isn't running" in out


def test_window_that_stays_open_is_reported(monkeypatch):
    win = {"hwnd": 5, "title": "*notes.txt - Notepad", "app": "Notepad"}
    monkeypatch.setattr("jarvis.tools.window_tools.list_windows", lambda: [win])  # a save prompt keeps it open
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([]))
    monkeypatch.setattr("win32gui.PostMessage", lambda *a: None)
    out = app_tools.close_app_named("notepad", {"notepad": "notepad.exe"}, wait=0.3)
    assert out.startswith("WARNING") and "save" in out


def test_closed_when_window_and_process_gone(monkeypatch):
    wins = [{"hwnd": 5, "title": "Spotify Premium", "app": "Spotify"}]
    proc = types.SimpleNamespace(info={"name": "Spotify.exe"}, is_running=lambda: False)
    sent = []
    monkeypatch.setattr("jarvis.tools.window_tools.list_windows", lambda: list(wins))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([proc]))
    monkeypatch.setattr("win32gui.PostMessage", lambda h, *a: (sent.append(h), wins.clear()))
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: (procs, []))
    assert app_tools.close_app_named("spotify", {"spotify": "spotify:"}).startswith("Closed spotify")
    assert sent == [5]


def test_progressive_claim_without_tool_is_caught():
    assert unbacked_claim("Closing notepad.")
    assert unbacked_claim("Okay, opening Spotify now.")
    assert not unbacked_claim("Closing remarks are usually short.", tools_ran=True)


def test_open_app_shows_an_already_running_app_instead_of_hiding_it(monkeypatch):
    """Live: 'Open Chrome' -> 'I can't see it' (Chrome was running, its window stayed behind)."""
    from jarvis.tools import app_tools, window_tools
    from jarvis.tools.registry import ToolRegistry

    launched, fronted = [], []
    wins = [{"hwnd": 7, "app": "chrome", "title": "New Tab - Google Chrome"}]
    monkeypatch.setattr(window_tools, "list_windows", lambda: list(wins))
    monkeypatch.setattr(app_tools, "_launch", launched.append)
    monkeypatch.setattr(app_tools, "bring_to_front", lambda h: fronted.append(h) or True)
    reg = ToolRegistry()
    app_tools.register(reg, {"chrome": "chrome.exe", "notepad": "notepad.exe"})
    open_app = reg.get("open_app").func
    assert open_app("chrome") == "chrome was already open: brought it to the front (checked)."
    assert launched == [] and fronted == [7]  # no second copy started

    def appear(target):  # notepad isn't running: launch it, its window appears, then bring it forward
        launched.append(target)
        wins.append({"hwnd": 9, "app": "Notepad", "title": "Untitled - Notepad"})

    monkeypatch.setattr(app_tools, "_launch", appear)
    assert open_app("notepad") == "Opened notepad (checked: its window is open and in front)."
    assert launched == ["notepad.exe"] and fronted == [7, 9]
