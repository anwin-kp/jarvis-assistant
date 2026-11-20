"""Transparent desktop overlay for the HUD (Qt WebEngine): `python -m jarvis.ui.overlay <hud url>`.

The same HUD page, in overlay mode (?overlay=1): no background, reactor in a corner, messages floating beside it.
Two modes, like game overlays:
  passive  - mouse clicks go through to your apps; replies appear and fade
  active   - the overlay takes clicks and keys: text box, buttons, approvals
Ctrl+Alt+J toggles active/passive, Esc returns to passive, Ctrl+Alt+H hides/shows it, Ctrl+Alt+D moves it to the
next display (remembered). It turns active by itself when
JARVIS needs an approval. Qt (signed DLLs) works with Smart App Control, unlike an unsigned packed .exe."""

import ctypes
import sys
import threading
from ctypes import wintypes

HOTKEY_ACTIVE, HOTKEY_HIDE, HOTKEY_MOVE = 1, 2, 3
STATE_FILE = __import__("pathlib").Path(__file__).resolve().parents[3] / "data" / "overlay.json"


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

    def place(i: int) -> None:
        sc = screens[i]
        view.setScreen(sc)
        view.move(sc.availableGeometry().topLeft())
        QTimer.singleShot(250, lambda: view.setGeometry(sc.availableGeometry()))

    place(current)
    state = {"active": False}

    def set_active(on: bool) -> None:
        state["active"] = on
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
            view.setVisible(not view.isVisible())
            if view.isVisible():
                set_active(state["active"])
        else:
            view.show()
            set_active(not state["active"])

    def on_title(title: str) -> None:
        """The page talks back through its title: it asks for active mode (approvals) or passive (Esc/close)."""
        if title.endswith(":active") and not state["active"]:
            set_active(True)
        elif title.endswith(":passive") and state["active"]:
            set_active(False)

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
    return app.exec()


def available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None))
