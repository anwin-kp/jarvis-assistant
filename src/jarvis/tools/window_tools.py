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


def layout_rects(area: tuple[int, int, int, int], n: int, layout: str = "side_by_side") -> list[tuple]:
    """Target (left, top, right, bottom) for n windows in a monitor's work area. side_by_side: columns (3 = one big
    left + two stacked right, 4 = 2x2 grid); stacked: rows."""
    left, top, right, bottom = area
    w, h = right - left, bottom - top
    midx, midy = left + w // 2, top + h // 2
    if n <= 1:
        return [area]
    if layout == "stacked" and n <= 3:
        rows = [top + h * i // n for i in range(n)] + [bottom]
        return [(left, rows[i], right, rows[i + 1]) for i in range(n)]
    if n == 2:
        return [(left, top, midx, bottom), (midx, top, right, bottom)]
    if n == 3:
        return [(left, top, midx, bottom), (midx, top, right, midy), (midx, midy, right, bottom)]
    return [(left, top, midx, midy), (midx, top, right, midy), (left, midy, midx, bottom), (midx, midy, right, bottom)]


def _frame_margins(hwnd: int) -> tuple[int, int, int, int]:
    """Windows 10/11 windows have invisible resize borders: GetWindowRect is bigger than what you see. Placing by
    GetWindowRect leaves ~7 px gaps; the visible frame comes from DWM."""
    import ctypes.wintypes as wt

    import win32gui

    try:
        vis = wt.RECT()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis))  # extended frame
        x1, y1, x2, y2 = win32gui.GetWindowRect(hwnd)
        return vis.left - x1, vis.top - y1, x2 - vis.right, y2 - vis.bottom
    except Exception:
        return 0, 0, 0, 0


def place_window(hwnd: int, target: tuple, screen: tuple) -> bool:
    """Put a window's VISIBLE frame on target (physical px) and check it. Measured on a 200% laptop + 100% monitor:
    a window whose (invisible) border crosses onto the other monitor gets re-scaled by Windows for that monitor's
    DPI (sizes came out doubled). So: move onto the screen first (its DPI settles), then size, never past the edge."""
    import win32con
    import win32gui

    tl, tt, tr, tb = target
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)  # a maximized window ignores a new position
    flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
    win32gui.SetWindowPos(hwnd, 0, tl, tt, 0, 0, flags | win32con.SWP_NOSIZE)
    time.sleep(0.1)
    for _ in range(2):  # a DPI change after the move can re-size it once: then set it again
        ml, mt, mr, mb = _frame_margins(hwnd)
        x1, y1 = max(screen[0], tl - ml), max(screen[1], tt - mt)
        x2, y2 = min(screen[2], tr + mr), min(screen[3], tb + mb)
        win32gui.SetWindowPos(hwnd, 0, x1, y1, x2 - x1, y2 - y1, flags | win32con.SWP_SHOWWINDOW)
        time.sleep(0.15)
        ml, mt, mr, mb = _frame_margins(hwnd)
        r = win32gui.GetWindowRect(hwnd)
        if max(abs(r[0] + ml - tl), abs(r[1] + mt - tt), abs(r[2] - mr - tr), abs(r[3] - mb - tb)) <= 24:
            return True
    return False


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

    @reg.tool("arrange_windows", "Put several open windows side by side on one screen, e.g. apps='chrome, code' "
              "('Chrome on the left and VS Code on the right'), in that order (first = left / top). 2-4 windows. "
              "layout: side_by_side (default; 3 = big left + two right, 4 = grid) or stacked (top/bottom).",
              {"apps": {"type": "string", "description": "comma-separated app names or window titles, in order"},
               "layout": {"type": "string", "enum": ["side_by_side", "stacked"]}},
              required=["apps"], group="windows")
    def arrange_windows(apps: str, layout: str = "side_by_side"):
        import win32api
        import win32con

        names = [a.strip() for a in apps.replace(" and ", ",").split(",") if a.strip()][:4]
        found, missing = [], []
        for n in names:
            w = find_window(n)
            (found.append(w) if w and w["hwnd"] not in [f["hwnd"] for f in found] else missing.append(n))
        if len(found) < 2 and not (len(found) == 1 and len(names) == 1):
            return f"ERROR: I need at least two open windows; not found: {', '.join(missing) or apps}."
        info = win32api.GetMonitorInfo(win32api.MonitorFromWindow(found[0]["hwnd"], win32con.MONITOR_DEFAULTTONEAREST))
        screen, targets = info["Monitor"], layout_rects(info["Work"], len(found), layout)  # work = minus taskbar
        off = []
        for w, target in zip(found, targets, strict=False):
            if not place_window(w["hwnd"], target, screen):
                off.append(w["app"])
        order = " | ".join(w["app"] for w in found)
        note = f" Not found: {', '.join(missing)}." if missing else ""
        if off:
            return (f"WARNING: arranged {order}, but {', '.join(off)} didn't take its place "
                    f"(it may limit its size).{note}")
        return f"Arranged {order} {'top to bottom' if layout == 'stacked' else 'side by side'} (checked).{note}"

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
