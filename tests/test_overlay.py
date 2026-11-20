from jarvis.ui.overlay import overlay_url


def test_overlay_url_adds_the_mode_flag():
    assert overlay_url("http://127.0.0.1:8765/?token=abc") == "http://127.0.0.1:8765/?token=abc&overlay=1"
    assert overlay_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765/?overlay=1"


def test_hud_falls_back_to_a_window_without_qt(monkeypatch):
    import queue

    from jarvis.ui import hud as hud_mod
    from jarvis.ui import overlay

    opened = []
    monkeypatch.setattr(overlay, "available", lambda: False)
    monkeypatch.setattr(hud_mod.subprocess, "Popen", lambda args, **kw: opened.append(args))
    h = hud_mod.Hud(queue.Queue())
    h.mode = "overlay"
    h.open()
    assert opened and "msedge" in opened[0]
