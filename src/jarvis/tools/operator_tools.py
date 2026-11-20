"""operate_app: hand a whole multi-step task in any app to the App operator (see jarvis/agent/operator.py)."""

import os
import threading
import time

from jarvis.core.config import get_secret
from jarvis.tools.registry import Risk, Tool, ToolRegistry

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
    risky_tool = Tool("app_action", "A step the app operator wants to take", {"type": "object", "properties": {},
                      "required": []}, Risk.DANGER, "screen", lambda: None)

    @reg.tool("operate_app", "Carry out a multi-step task inside any app by looking at it and clicking/typing like a "
              "person: e.g. app='spotify' task='open my Mix-Up Mash playlist and play it', app='chrome' task='open "
              "gmail and search for invoices'. Opens the app if needed.",
              {"app": {"type": "string", "description": "app or window name"},
               "task": {"type": "string", "description": "what to do, step goal in plain words"}},
              risk=Risk.CONFIRM, group="screen")
    def operate_app(app: str, task: str):
        from jarvis.agent.operator import Operator

        key = get_secret("groq_api_key")
        if not key:
            return "ERROR: operating apps needs the Groq key (vision model). Run: .\\jarvis.ps1 set-key groq"
        hwnd = _find_or_open(app, allow)
        if not hwnd:
            return f"ERROR: couldn't find or open '{app}'."
        reg.operator_cancel.clear()
        confirm = getattr(reg, "confirm_fn", None)
        op = Operator(key, ocfg.get("model", "qwen/qwen3.8-27b"), ocfg.get("max_steps", 15),
                      confirm=(lambda what: bool(confirm and confirm(risky_tool, {"action": what}))),
                      cancel=reg.operator_cancel)
        return op.run(task, hwnd)

    @reg.tool("press_keys", "Press a keyboard shortcut in the app the user is working in, e.g. 'ctrl+s', 'ctrl+t', "
              "'alt+tab', 'f5', 'enter'.", {"keys": {"type": "string"}}, risk=Risk.CONFIRM, group="screen")
    def press_keys(keys: str):
        from pywinauto import keyboard

        from jarvis.agent.operator import to_send_keys

        keyboard.send_keys(to_send_keys(keys))
        return f"Pressed {keys}."
