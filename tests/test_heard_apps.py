"""Misheard app names still work as instant commands; unrelated words never match an app."""
from jarvis.core.fast_intents import FastIntents, heard_app

APPS = {"calculator", "chrome", "edge", "file explorer", "notepad", "settings", "spotify", "task manager", "vs code"}


def test_sound_alikes_resolve():
    for said, app in [("not padder", "notepad"), ("spotty fly", "spotify"), ("calculater", "calculator"),
                      ("crome", "chrome"), ("Notepad", "notepad")]:
        assert heard_app(said, APPS) == app, said


def test_unrelated_words_dont_match():
    for said in ["the door", "this tab", "the window", "my email", "music", "word", "it", "pad"]:
        assert heard_app(said, APPS) is None, said


def test_closed_not_padder_closes_notepad():
    ran = []

    class Reg:
        def get(self, name):
            return object()

        def execute(self, name, args, confirm):
            ran.append((name, args))
            return type("R", (), {"text": "Closed notepad (checked: no window or process left).", "error": False})()

    fast = FastIntents(Reg(), {"security": {"app_allowlist": {a: a for a in APPS}}})
    assert fast.try_handle("Closed not padder.", None) == "Closed notepad."
    assert ran == [("close_app", {"name": "notepad"})]
