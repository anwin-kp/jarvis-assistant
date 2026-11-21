"""Transparent desktop overlay for the HUD (Qt WebEngine): `python -m jarvis.ui.overlay <hud url>`.

The same HUD page, in overlay mode (?overlay=1): no background, reactor in a corner, messages floating beside it.
Two modes, like game overlays:
  passive  - mouse clicks go through to your apps; replies appear and fade
  active   - the overlay takes clicks and keys: text box, buttons, approvals
It hides by itself while a game / fullscreen video is in front or Gaming mode is on.
Ctrl+Alt+J toggles active/passive (and shows it even then), Esc returns to passive, Ctrl+Alt+H hides/shows it,
Ctrl+Alt+D moves it to the next display (remembered). It turns active by itself when JARVIS needs an approval.
Qt (signed DLLs) works with Smart App Control, unlike an unsigned packed .exe."""

import ctypes
import sys
import threading
from ctypes import wintypes

HOTKEY_ACTIVE, HOTKEY_HIDE, HOTKEY_MOVE = 1, 2, 3
STATE_FILE = __import__("pathlib").Path(__file__).resolve().parents[3] / "data" / "overlay.json"


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}  # desktop / taskbar


def fullscreen_in_front(own_hwnd: int) -> bool:
    """A game, fullscreen video or F11 browser is in front on the overlay's display: its window covers the whole
    monitor, taskbar area included (a normal maximized window doesn't)."""
    import win32api
    import win32con
    import win32gui

    fg = win32gui.GetForegroundWindow()
    if not fg or fg == own_hwnd or win32gui.GetClassName(fg) in SHELL_CLASSES:
        return False
    nearest = win32con.MONITOR_DEFAULTTONEAREST
    mon = win32api.MonitorFromWindow(fg, nearest)
    if int(mon) != int(win32api.MonitorFromWindow(own_hwnd, nearest)):
        return False
    left, top, right, bottom = win32gui.GetWindowRect(fg)
    m = win32api.GetMonitorInfo(mon)["Monitor"]
    return left <= m[0] and top <= m[1] and right >= m[2] and bottom >= m[3]


def saved_screen() -> str | None:
    import json

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("screen")
    except (OSError, ValueError):
        return None


def save_screen(name: str) -> None:
    import json

    try:
        STATE_FILE.write_text(json.dumps({"screen": name}), encoding="utf-8")
    except OSError:
        pass
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x20, 0x80000
TITLE = "J.A.R.V.I.S. overlay"


def single_instance() -> bool:
    import win32api
    import win32event
    import winerror

    single_instance.lock = win32event.CreateMutex(None, False, "Local\\JARVIS-overlay")
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def parse_strip(title: str) -> tuple[str, int] | None:
    """'JARVIS:7:strip-r-748' -> ('r', 748): the page's message strip (side, width in CSS px)."""
    import re

    m = re.search(r":strip-([lr])-(\d{2,5})$", title)
    return (m.group(1), int(m.group(2))) if m else None


def strip_geometry(screen_rect, side: str, width: int, active: bool):
    """Passive: a full-height strip on the overlay's side; active (typing, settings): the whole display."""
    if active or width <= 0 or width >= screen_rect.width():
        return screen_rect
    rect = type(screen_rect)(screen_rect)
    if side == "r":
        rect.setLeft(screen_rect.right() - width + 1)
    else:
        rect.setRight(screen_rect.left() + width - 1)
    return rect


def overlay_url(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "overlay=1"


def click_through(hwnd: int, on: bool) -> None:
    """Passive mode: Windows sends mouse input to the windows underneath."""
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
    style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def main(url: str, owner_pid: int | None = None) -> int:
    if not single_instance():
        return 0
    if sys.stderr is None:  # pythonw: keep errors (Qt slot exceptions included) in data/logs/overlay.log
        log = STATE_FILE.parent / "logs" / "overlay.log"
        sys.stdout = sys.stderr = open(log, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS")

    class Bridge(QObject):
        hotkey = Signal(int)

    bridge = Bridge()
    view = QWebEngineView()
    profile = QWebEngineProfile.defaultProfile()
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    view.page().setBackgroundColor(QColor(0, 0, 0, 0))
    view.setWindowTitle(TITLE)
    view.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                        | Qt.WindowType.Tool)  # Tool: no taskbar button, like an overlay
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    view.setStyleSheet("background: transparent")
    # which display: the one chosen last time (Ctrl+Alt+D), else the primary one
    # Which display: the one chosen last time (Ctrl+Alt+D), else the primary one. Displays can have different
    # scaling (laptop 200%, monitor 100%): move onto the display first, size it once Qt uses that display's scale,
    # otherwise the size comes out doubled.
    screens = app.screens()
    names = [sc.name() for sc in screens]
    current = names.index(saved_screen()) if saved_screen() in names else screens.index(app.primaryScreen())

    state = {"active": False}
    # passive: only the strip the page draws in (the page says how wide, and on which side); active: whole display
    strip = {"side": "r", "width": 0}

    def geometry():
        return strip_geometry(screens[current].availableGeometry(), strip["side"], strip["width"], state["active"])

    def place(i: int) -> None:
        sc = screens[i]
        view.setScreen(sc)
        view.move(sc.availableGeometry().topLeft())
        QTimer.singleShot(250, lambda: view.setGeometry(geometry()))

    place(current)
    # why it is hidden: "user" (Ctrl+Alt+H), "fullscreen" (a game/video in front), "gaming" (Gaming mode)
    hidden_by: set[str] = set()

    def update_visibility() -> None:
        want = not hidden_by or (state["active"] and "user" not in hidden_by)  # Ctrl+Alt+J shows it anyway
        if want and not view.isVisible():
            view.show()
            set_active(state["active"])
        elif not want and view.isVisible():
            view.hide()

    def set_active(on: bool) -> None:
        state["active"] = on
        if view.geometry() != geometry():
            view.setGeometry(geometry())
        click_through(int(view.winId()), not on)
        view.page().runJavaScript(f"window.overlayActive && overlayActive({'true' if on else 'false'})")
        if on:
            view.raise_()
            view.activateWindow()
            ctypes.windll.user32.SetForegroundWindow(int(view.winId()))

    def on_hotkey(which: int) -> None:
        if which == HOTKEY_MOVE:  # next display, remembered for next time
            nonlocal current
            current = (current + 1) % len(screens)
            place(current)
            save_screen(screens[current].name())
            return
        if which == HOTKEY_HIDE:
            if view.isVisible():
                hidden_by.add("user")
            else:
                hidden_by.discard("user")
            update_visibility()
        else:
            hidden_by.discard("user")
            set_active(not state["active"])
            update_visibility()

    def on_title(title: str) -> None:
        """The page talks back through its title: it asks for active mode (approvals) or passive (Esc/close)."""
        if title.endswith(":active") and not state["active"]:
            set_active(True)
            update_visibility()
        elif title.endswith(":passive") and state["active"]:
            set_active(False)
            update_visibility()
        elif title.endswith(":gaming-on"):
            hidden_by.add("gaming")
            update_visibility()
        elif title.endswith(":gaming-off"):
            hidden_by.discard("gaming")
            update_visibility()
        elif title.endswith(":toggle-hidden"):  # tray menu: same as the hotkeys
            on_hotkey(HOTKEY_HIDE)
        elif title.endswith(":activate"):
            hidden_by.discard("user")
            set_active(True)
            update_visibility()
        elif title.endswith(":next-display"):
            on_hotkey(HOTKEY_MOVE)
        elif (got := parse_strip(title)) and got != (strip["side"], strip["width"]):
            strip["side"], strip["width"] = got
            if not state["active"]:
                view.setGeometry(geometry())

    bridge.hotkey.connect(on_hotkey)
    view.titleChanged.connect(on_title)
    view.loadFinished.connect(lambda ok: set_active(state["active"]))
    view.load(QUrl(overlay_url(url)))
    view.show()
    QTimer.singleShot(300, lambda: set_active(False))

    def hotkeys() -> None:
        """Global hotkeys need a message loop on the thread that registered them."""
        user32 = ctypes.windll.user32
        for hid, key in ((HOTKEY_ACTIVE, "J"), (HOTKEY_HIDE, "H"), (HOTKEY_MOVE, "D")):
            if not user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord(key)):
                print(f"Ctrl+Alt+{key} is already used by another app; that overlay hotkey is unavailable", flush=True)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                bridge.hotkey.emit(int(msg.wParam))

    threading.Thread(target=hotkeys, daemon=True, name="hotkeys").start()

    def owner_alive() -> None:  # the overlay belongs to one JARVIS: close with it (restart = fresh overlay + page)
        import psutil

        if owner_pid and not psutil.pid_exists(owner_pid):
            app.quit()

    alive = QTimer()
    alive.timeout.connect(owner_alive)
    alive.start(2000)

    def watch_fullscreen() -> None:
        try:
            full = fullscreen_in_front(int(view.winId()))
        except Exception:
            return
        if full != ("fullscreen" in hidden_by):
            (hidden_by.add if full else hidden_by.discard)("fullscreen")
            update_visibility()

    fullscreen_timer = QTimer()
    fullscreen_timer.timeout.connect(watch_fullscreen)
    fullscreen_timer.start(1000)
    return app.exec()


def available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None))
