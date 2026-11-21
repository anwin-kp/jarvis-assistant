"""Open and close applications from an allowlist."""

import os
import subprocess
from pathlib import Path

import psutil

from jarvis.tools.registry import Risk, ToolRegistry


def _launch(target: str) -> None:
    try:
        os.startfile(target)  # handles .exe in App Paths, URIs like spotify:, ms-settings:
    except OSError:
        # `start` also searches PATH (e.g. VS Code's `code` command)
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=subprocess.CREATE_NO_WINDOW)


# The process a launch target really runs as (Store apps and URIs don't match their launch name)
REAL_EXE = {"calc": "calculatorapp", "spotify:": "spotify", "ms-settings:": "systemsettings",
            "mspaint": "mspaint", "wt": "windowsterminal"}
# Never "close" these: the desktop shell, Windows itself, or JARVIS
PROTECTED = {"explorer", "dwm", "csrss", "winlogon", "svchost", "lsass", "services", "applicationframehost",
             "python", "pythonw", "jarvis", "taskmgr"}


def app_names(key: str, target: str) -> set[str]:
    """Process names (no .exe, lowercase) an app may run as: the name said, its launch target, known aliases."""
    names = {key}
    t = target.lower().strip()
    stem = t if t.endswith(":") else Path(t).name.removesuffix(".exe")
    names |= {stem.rstrip(":"), REAL_EXE.get(stem, stem).rstrip(":")}
    return {n for n in names if n} - PROTECTED


def _is_app_window(w: dict, key: str, names: set[str]) -> bool:
    app = w["app"].lower()
    if app in names:
        return True
    # Store apps (Calculator, Settings, ...): the window belongs to ApplicationFrameHost; match its title
    return app == "applicationframehost" and key in w["title"].lower()


def close_app_named(name: str, apps: dict[str, str], wait: float = 5.0) -> str:
    """Close an app the way a person would (its windows' close button), check its windows are really gone, then
    end what's left in the background (e.g. Spotify in the tray). Unsaved work is never force-closed."""
    import win32con
    import win32gui

    from jarvis.tools.registry import wait_for
    from jarvis.tools.window_tools import list_windows

    key = name.lower().strip().removesuffix(".exe")
    if key in PROTECTED:
        return f"ERROR: I won't close {name}: it's part of Windows or JARVIS."
    names = app_names(key, apps.get(key, key))
    windows = [w for w in list_windows() if _is_app_window(w, key, names)]
    procs = [p for p in psutil.process_iter(["name"])
             if (p.info.get("name") or "").lower().removesuffix(".exe") in names]
    if not windows and not procs:
        return f"ERROR: {name} isn't running (no window or process found), so nothing was closed."
    for w in windows:
        try:
            win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
        except Exception:  # the window closed by itself meanwhile
            pass

    def open_windows():
        return [w for w in list_windows() if _is_app_window(w, key, names)]

    if windows and not wait_for(lambda: not open_windows(), timeout=wait, every=0.3):
        left = open_windows()
        title = left[0]["title"] if left else name
        return (f"WARNING: I asked {name} to close, but its window is still open ('{title}'). It may be asking "
                f"to save changes, so I didn't force it (unsaved work would be lost). Tell the user to check it.")
    rest = [p for p in procs if p.is_running()]
    for p in rest:  # windows are closed; what's left runs in the background (tray, helpers)
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(rest, timeout=3)
    if alive:
        return f"WARNING: {name}'s windows are closed, but {len(alive)} of its processes are still running."
    return f"Closed {name} (checked: no window or process left)."


def register(reg: ToolRegistry, app_allowlist: dict[str, str]) -> None:
    apps = {k.lower(): v for k, v in app_allowlist.items()}
    names = ", ".join(sorted(apps))

    @reg.tool(
        "open_app",
        f"Open an application. Known apps: {names}.",
        {"name": {"type": "string", "description": "App name from the known list"}},
        group="system",
    )
    def open_app(name: str):
        key = name.lower().strip()
        if key not in apps:
            return f"ERROR: '{name}' is not in my app list. Known apps: {names}. The user can add it to config.yaml."
        _launch(apps[key])
        from jarvis.tools.registry import wait_for
        from jarvis.tools.window_tools import find_window

        if wait_for(lambda: find_window(name), timeout=6):
            return f"Opened {name} (checked: its window is open)."
        return f"Started {name}, but no window has appeared yet (checked for 6 s); it may still be loading."

    @reg.tool(
        "close_app",
        "Close (gracefully terminate) a running application by name, e.g. 'chrome' or 'notepad'.",
        {"name": {"type": "string"}},
        risk=Risk.CONFIRM,
        group="system",
    )
    def close_app(name: str):
        return close_app_named(name, apps)
