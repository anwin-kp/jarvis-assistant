"""Silent launcher for the desktop shortcut / autostart. Run with pythonw (no console):
    pythonw -m jarvis.launcher
It runs start-jarvis.ps1 hidden. You only ever see the HUD, or a message box if startup fails."""

import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "start-jarvis.ps1"),
         *sys.argv[1:]],
        capture_output=True, text=True, cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if r.returncode != 0:
        lines = [ln.strip() for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
        ctypes.windll.user32.MessageBoxW(None, "\n".join(lines[-10:]) or "JARVIS failed to start.",
                                         "JARVIS could not start", 0x10)


if __name__ == "__main__":
    main()
