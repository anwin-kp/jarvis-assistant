"""Fixed voice/text commands handled before the AI model sees the text (instant, never misinterpreted)."""

import re
import subprocess
from pathlib import Path

# "Jarvis shut down", "Hey Jarvis, shutdown now", "Shutdown Jarvis", "Shut down the Jarvis please"...
# Deliberately NOT matched: "Jarvis, shut down the computer" (that's the PC shutdown tool, which asks first).
_KILL = [
    re.compile(r"^\W*(hey\W+)?jarvis\W*(please\W+)?shut[\s-]?down(\W+(now|please|yourself|completely))*\W*$", re.I),
    re.compile(r"^\W*(please\W+)?shut[\s-]?down\W+(the\W+)?jarvis(\W+(now|please|completely))*\W*$", re.I),
]

KILL_SCRIPT = Path(__file__).resolve().parents[3] / "kill-jarvis.cmd"


# Right after the wake word ("Hey Jarvis" ... "shut down") the recording may only contain "shut down".
_KILL_AFTER_WAKE = re.compile(r"^\W*(please\W+)?shut[\s-]?down(\W+(now|please|yourself|completely))*\W*$", re.I)


def is_kill_command(text: str, after_wake: bool = False) -> bool:
    t = text.strip()
    return any(p.match(t) for p in _KILL) or (after_wake and bool(_KILL_AFTER_WAKE.match(t)))


_SLEEP = re.compile(
    r"^\W*(hey\W+)?(jarvis\W*)?(please\W+)?(go\W+to\W+sleep|stop\W+listening|sleep(\W+mode)?|mute\W+(the\W+)?mic(rophone)?)"
    r"(\W+jarvis)?\W*$", re.I)


def is_sleep_command(text: str) -> bool:
    """'Jarvis, go to sleep' / 'stop listening': pause always-listening until 'Hey Jarvis'."""
    return bool(_SLEEP.match(text.strip()))


def run_kill_switch(visible: bool = True) -> None:
    """Run the kill switch (in its own window, or silently). It force-stops this process."""
    if visible:
        subprocess.Popen(["cmd", "/c", str(KILL_SCRIPT)], creationflags=subprocess.CREATE_NEW_CONSOLE,
                         cwd=str(KILL_SCRIPT.parent))
    else:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(KILL_SCRIPT.with_suffix(".ps1"))], creationflags=subprocess.CREATE_NO_WINDOW,
                         cwd=str(KILL_SCRIPT.parent))
