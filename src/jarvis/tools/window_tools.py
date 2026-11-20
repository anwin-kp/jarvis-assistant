"""Window management: list, focus/switch, minimize, maximize, snap, close, show desktop."""

import ctypes
import time

import psutil

from jarvis.tools.registry import Risk, ToolRegistry

VK_LWIN, VK_LEFT, VK_RIGHT, VK_MENU = 0x5B, 0x25, 0x27, 0x12


def list_windows() -> list[dict]:
    import win32gui
    import win32process

    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not (title := win32gui.GetWindowText(hwnd).strip()):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app = psutil.Process(pid).name().removesuffix(".exe")
        except Exception:
            app = ""
        if title != "Program Manager":
            out.append({"hwnd": hwnd, "title": title, "app": app})

    win32gui.EnumWindows(cb, None)
    return out


def find_window(name: str) -> dict | None:
    n = name.lower().strip()
    wins = list_windows()
    for w in wins:  # app name first ("chrome", "code"), then title text
        if w["app"].lower() == n:
            return w
    return next((w for w in wins if n in w["title"].lower() or n in w["app"].lower()), None)


def focus(hwnd: int) -> None:
    import win32con
    import win32gui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)  # Alt tap lets us take the foreground
    ctypes.windll.user32.keybd_event(VK_MENU, 0, 2, 0)
    win32gui.SetForegroundWindow(hwnd)


def _combo(*keys: int) -> None:
    for k in keys:
        ctypes.windll.user32.keybd_event(k, 0, 0, 0)
    for k in reversed(keys):
        ctypes.windll.user32.keybd_event(k, 0, 2, 0)


def register(reg: ToolRegistry) -> None:
    @reg.tool("list_windows", "List open windows (app and title).", {}, group="windows", read_only=True)
    def _list():
        return [f"{w['app']}: {w['title'][:80]}" for w in list_windows()][:40]

    @reg.tool("window_action", "Act on an open window by app or title, e.g. name='chrome' action='focus'. "
              "Actions: focus (switch to it), minimize, maximize, restore, snap_left, snap_right.",
              {"name": {"type": "string"},
               "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "snap_left",
                                                     "snap_right"]}},
              group="windows")
    def window_action(name: str, action: str):
        import win32con
        import win32gui

        w = find_window(name)
        if not w:
            return f"ERROR: no open window matching '{name}'."
        h = w["hwnd"]
        if action in ("focus", "snap_left", "snap_right"):
            focus(h)
            if action != "focus":
                time.sleep(0.15)
                _combo(VK_LWIN, VK_LEFT if action == "snap_left" else VK_RIGHT)
        else:
            win32gui.ShowWindow(h, {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
                                    "restore": win32con.SW_RESTORE}[action])
        return f"{action.replace('_', ' ').capitalize()}: {w['app']} ({w['title'][:50]})."

    @reg.tool("close_window", "Close an open window by app or title (unsaved work may prompt).",
              {"name": {"type": "string"}}, risk=Risk.CONFIRM, group="windows")
    def close_window(name: str):
        import win32con
        import win32gui

        w = find_window(name)
        if not w:
            return f"ERROR: no open window matching '{name}'."
        win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
        return f"Asked {w['app']} ({w['title'][:50]}) to close."

    @reg.tool("show_desktop", "Minimize all windows / show the desktop (toggle).", {}, group="windows")
    def show_desktop():
        _combo(VK_LWIN, 0x44)  # Win+D
        return "Toggled the desktop."
