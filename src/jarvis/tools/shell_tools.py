"""Optional PowerShell tool. OFF by default (security.shell_enabled). Read-only commands only."""

import re
import subprocess

from jarvis.tools.registry import Risk, ToolRegistry

ALLOWED_PREFIXES = (
    "get-process", "get-service", "get-childitem", "get-content", "get-date", "get-computerinfo",
    "get-netipaddress", "get-netadapter", "get-volume", "get-psdrive", "get-hotfix", "get-wmiobject",
    "get-ciminstance", "test-connection", "ipconfig", "ping", "systeminfo", "whoami", "hostname",
    "winget list", "winget search", "tasklist", "netstat", "nslookup",
)
BLOCKED = re.compile(
    r"remove-|del\b|rm\b|rmdir|format-volume|format\s|set-|new-|stop-|start-|restart-|invoke-expression|\biex\b|"
    r"invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b|downloadstring|-encodedcommand|-enc\b|\breg\b|bcdedit|"
    r"diskpart|cipher|takeown|icacls|shutdown|[;&|`>]|\$\(",
    re.IGNORECASE,
)


def check_command(command: str) -> str | None:
    """Return an error message if the command is not allowed, else None."""
    c = command.strip().lower()
    if not c.startswith(ALLOWED_PREFIXES):
        return "Only read-only commands are allowed (e.g. Get-Process, ipconfig, systeminfo)."
    if BLOCKED.search(c):
        return "That command contains a blocked operation, pipe or redirect."
    return None


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "run_powershell",
        "Run a read-only PowerShell command (Get-*, ipconfig, ping, systeminfo...). No pipes or changes.",
        {"command": {"type": "string"}},
        risk=Risk.DANGER,
        group="system",
    )
    def run_powershell(command: str):
        if err := check_command(command):
            raise PermissionError(err)
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return (r.stdout or r.stderr or "(no output)")[-4000:]
