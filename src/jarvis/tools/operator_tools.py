"""operate_app: hand a whole multi-step task in any app to the App operator (see jarvis/agent/operator.py)."""

import os
import threading
import time

from loguru import logger

from jarvis.agent.uia import on_uia_thread
from jarvis.core.config import get_secret
from jarvis.tools.registry import Risk, ToolRegistry

APP_URIS = {"spotify": "spotify:", "settings": "ms-settings:", "store": "ms-windows-store:", "mail": "outlookmail:",
            "calculator": "calc.exe", "notepad": "notepad.exe", "paint": "mspaint.exe", "explorer": "explorer.exe"}


def _find_or_open(app: str, allowlist: dict[str, str], wait: float = 12):
    from jarvis.tools.window_tools import find_window

    w = find_window(app)
    if w:
        return w["hwnd"]
    target = allowlist.get(app.lower()) or APP_URIS.get(app.lower())
    if not target:
        return None
    os.startfile(target)
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1)
        if w := find_window(app):
            time.sleep(1.5)  # let it finish drawing
            return w["hwnd"]
    return None


def register(reg: ToolRegistry, cfg: dict) -> None:
    ocfg = cfg.get("operator") or {}
    if not ocfg.get("enabled", True):
        return
    reg.operator_cancel = threading.Event()
    allow = {k.lower(): v for k, v in (cfg.get("security", {}).get("app_allowlist") or {}).items()}

    @reg.tool("operate_app", "Carry out a multi-step task inside any app by looking at it and clicking/typing like a "
              "person, e.g. app='settings' task='turn on night light', app='chrome' task='open gmail and search for "
              "invoices'. Opens the app if needed. NOT for playing music: use spotify_play (it opens Spotify itself).",
              {"app": {"type": "string", "description": "app or window name"},
               "task": {"type": "string", "description": "what to do, step goal in plain words"}},
              risk=Risk.CONFIRM, group="screen")
    @on_uia_thread  # pywinauto needs its own COM thread (jarvis/agent/uia.py)
    def operate_app(app: str, task: str):
        from jarvis.agent.operator import Operator

        key = get_secret("groq_api_key")
        if not key:
            return "ERROR: operating apps needs the Groq key (vision model). Run: .\\jarvis.ps1 set-key groq"
        hwnd = _find_or_open(app, allow)
        if not hwnd:
            return f"ERROR: couldn't find or open '{app}'."
        reg.operator_cancel.clear()
        op = Operator(key, ocfg.get("model", "qwen/qwen3.8-27b"), ocfg.get("max_steps", 15),
                      confirm=reg.ask,  # asks only before risky clicks (buy, delete, send...) and closing
                      cancel=reg.operator_cancel)
        try:
            return op.run(task, hwnd)
        except Exception as e:
            logger.exception("operate_app failed")
            return (f"ERROR: the task in {app} failed partway ({type(e).__name__}). Tell the user it didn't work; "
                    "do not guess what the app shows.")

    @reg.tool("press_keys", "Press ONE keyboard shortcut in the app the user is working in, e.g. 'ctrl+s', "
              "'ctrl+t', 'alt+tab', 'f5'. For typing, calculations or anything with several steps in an app, "
              "use operate_app instead.", {"keys": {"type": "string"}}, risk=Risk.CONFIRM, group="screen")
    @on_uia_thread  # pywinauto needs its own COM thread (jarvis/agent/uia.py)
    def press_keys(keys: str):
        from pywinauto import keyboard

        from jarvis.agent.operator import to_send_keys

        if keys.lower().replace(" ", "") in ("alt+f4", "ctrl+w", "ctrl+q", "ctrl+shift+w") and \
                not reg.ask(f"press {keys} (closes something)"):
            return f"Not pressed: {keys} closes things and you didn't approve it."
        keyboard.send_keys(to_send_keys(keys))
        return f"Pressed {keys}."
