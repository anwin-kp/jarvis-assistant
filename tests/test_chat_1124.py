"""Regressions from the live chat of 24 Nov 2026, 00:33-00:38."""
import psutil

from jarvis.core.orchestrator import required_tools
from jarvis.llm.ollama_brain import CLAIMS_ACTION
from jarvis.stt.lexicon import Lexicon
from jarvis.tools import app_tools

TWO = [{"hwnd": 1, "title": "main.dart - quickop_mobile_app - Visual Studio Code", "app": "Code"},
       {"hwnd": 2, "title": "orchestrator.py - Jarvis-dev - Visual Studio Code", "app": "Code"}]


def test_close_app_never_closes_every_window_unasked(monkeypatch):
    """'Close QuickOP mobile in VS Code' -> close_app('code') closed BOTH VS Code windows."""
    sent = []
    monkeypatch.setattr("jarvis.tools.window_tools.list_windows", lambda: list(TWO))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([]))
    monkeypatch.setattr("win32gui.PostMessage", lambda h, *a: sent.append(h))
    out = app_tools.close_app_named("code", {"vs code": "code"})
    assert out.startswith("ERROR: nothing closed") and "quickop_mobile_app" in out and "close_window" in out
    assert sent == []
    app_tools.close_app_named("code", {"vs code": "code"}, wait=0.2, every_window=True)  # asked for all
    assert sent == [1, 2]


def test_misheard_quickop_mobile_is_corrected():
    L = Lexicon()
    L.add(["Quick OP Mobile"], "project", private=True)
    assert L.correct("Open QCopy Mobile Folder in VS Code.", count=False) == "Open Quick OP Mobile Folder in VS Code."
    assert L.correct("Close QCOPI mobile.", count=False) == "Close Quick OP Mobile."
    for s in ["copy the file to my desktop", "open my mobile settings", "close the copy window"]:
        assert L.correct(s, count=False) == s


def test_score_needs_a_sports_question():
    tools = {"web_search", "open_in_browser"}
    assert required_tools("I lost a QCOPI mobile, while I'm via score.", tools) == set()
    assert required_tools("what's the score of the india match", tools) == {"web_search"}
    assert required_tools("live cricket score", tools) == {"web_search"}


def test_open_a_site_in_chrome_uses_the_users_browser():
    tools = {"open_in_browser", "browser_open"}
    assert required_tools("Open WhatsApp Web in Chrome", tools) == {"open_in_browser"}
    assert required_tools("open youtube in chrome and search for lofi", tools) == set()
    assert required_tools("open notepad", tools) == set()


def test_is_open_in_chrome_is_a_claim():
    assert CLAIMS_ACTION.search("WhatsApp Web is open in Chrome.")
    assert CLAIMS_ACTION.search("Spotify is now playing on Spotify.")
    assert not CLAIMS_ACTION.search("The shop is open in the mornings.")
