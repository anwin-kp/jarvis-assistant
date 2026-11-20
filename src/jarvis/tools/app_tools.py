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
        return f"Opened {name}."

    @reg.tool(
        "close_app",
        "Close (gracefully terminate) a running application by name, e.g. 'chrome' or 'notepad'.",
        {"name": {"type": "string"}},
        risk=Risk.CONFIRM,
        group="system",
    )
    def close_app(name: str):
        key = name.lower().strip()
        target = apps.get(key, key)
        exe = Path(target).name.lower()
        if not exe.endswith(".exe"):
            exe += ".exe"
        procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == exe]
        if not procs:
            return f"No running process named {exe}."
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=5)
        if alive:
            return f"Asked {exe} to close; {len(alive)} process(es) are still running."
        return f"Closed {exe} ({len(procs)} process(es))."
