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


def test_fullscreen_detection(monkeypatch):
    import types

    from jarvis.ui import overlay

    mon = (0, 0, 2560, 1600)
    fake = {"fg": 5, "cls": "Chrome_WidgetWin_1", "rect": mon, "mon_fg": 1, "mon_own": 1}
    gui = types.SimpleNamespace(GetForegroundWindow=lambda: fake["fg"], GetClassName=lambda h: fake["cls"],
                                GetWindowRect=lambda h: fake["rect"])
    api = types.SimpleNamespace(MonitorFromWindow=lambda h, f: fake["mon_fg"] if h == 5 else fake["mon_own"],
                                GetMonitorInfo=lambda m: {"Monitor": mon})
    con = types.SimpleNamespace(MONITOR_DEFAULTTONEAREST=2)
    monkeypatch.setitem(__import__("sys").modules, "win32gui", gui)
    monkeypatch.setitem(__import__("sys").modules, "win32api", api)
    monkeypatch.setitem(__import__("sys").modules, "win32con", con)
    assert overlay.fullscreen_in_front(9)  # a game covering the whole monitor
    fake["rect"] = (-8, -8, 2568, 1512)  # maximized window: the taskbar stays visible
    assert not overlay.fullscreen_in_front(9)
    fake.update(rect=mon, cls="WorkerW")  # the desktop itself
    assert not overlay.fullscreen_in_front(9)
    fake.update(cls="Game", mon_fg=2)  # fullscreen, but on the other display
    assert not overlay.fullscreen_in_front(9)


def test_overlay_settings_keep_only_valid_choices(monkeypatch, tmp_path):
    from jarvis.ui import hud

    monkeypatch.setattr(hud, "OVERLAY_SETTINGS", tmp_path / "s.json")
    assert hud.load_overlay_settings() == hud.OVERLAY_DEFAULTS
    out = hud.save_overlay_settings({"corner": "tl", "size": "l", "fade": "never", "evil": "x", "width": "huge"})
    assert out == {"corner": "tl", "size": "l", "width": "normal", "fade": "never"}
    assert hud.load_overlay_settings() == out
