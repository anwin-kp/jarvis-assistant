"""Vision: cloud first, the local model when the cloud fails or there's no key, else nothing (then OCR)."""

import requests
from PIL import Image

from jarvis.tools import vision_tools

IMG = Image.new("RGB", (64, 64))


def test_cloud_first(monkeypatch):
    monkeypatch.setattr(vision_tools, "get_secret", lambda k: "gsk_x")
    monkeypatch.setattr(vision_tools, "ask_vision", lambda *a, **k: "a login form")
    monkeypatch.setattr(vision_tools, "ask_local_vision", lambda *a, **k: 1 / 0)
    assert vision_tools.see(IMG, "what is this", {}) == ("a login form", "cloud")


def test_local_when_the_cloud_fails_or_no_key(monkeypatch):
    def down(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(vision_tools, "get_secret", lambda k: "gsk_x")
    monkeypatch.setattr(vision_tools, "ask_vision", down)
    monkeypatch.setattr(vision_tools, "ask_local_vision", lambda *a, **k: "an error dialog")
    assert vision_tools.see(IMG, "q", {}) == ("an error dialog", "local")
    monkeypatch.setattr(vision_tools, "get_secret", lambda k: None)
    assert vision_tools.see(IMG, "q", {}) == ("an error dialog", "local")


def test_nothing_when_both_are_unavailable_or_local_is_off(monkeypatch):
    monkeypatch.setattr(vision_tools, "get_secret", lambda k: None)

    def missing(*a, **k):
        raise LookupError("not downloaded")

    monkeypatch.setattr(vision_tools, "ask_local_vision", missing)
    assert vision_tools.see(IMG, "q", {}) == ("", "none")
    monkeypatch.setattr(vision_tools, "ask_local_vision", lambda *a, **k: "x")
    assert vision_tools.see(IMG, "q", {"vision": {"local_fallback": False}}) == ("", "none")
