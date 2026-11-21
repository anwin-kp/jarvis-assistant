"""Everyday PC questions answered in one call: is the internet working, what did I just download / open,
open this site in MY browser."""

import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

from jarvis.agent.uia import on_uia_thread
from jarvis.tools.registry import ToolRegistry

BROWSER_EXE = {"chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe",
               "microsoft edge": "msedge.exe", "brave": "brave.exe", "firefox": "firefox.exe"}


def local_ip() -> str | None:
    """The address this PC uses on the network (a UDP 'connect' sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: never routed anywhere
            return s.getsockname()[0]
    except OSError:
        return None


def wifi_info(netsh_output: str) -> dict:
    """SSID / signal / state from `netsh wlan show interfaces` (English Windows)."""
    out = {}
    for line in netsh_output.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        k, v = key.strip().lower(), val.strip()
        if k == "ssid":
            out["wifi_name"] = v
        elif k == "signal":
            out["wifi_signal"] = v
        elif k == "state":
            out["wifi_state"] = v
        elif k in ("receive rate (mbps)", "transmit rate (mbps)"):
            out[k.replace(" (mbps)", "_mbps").replace(" ", "_")] = v
    return out


def internet_check(urls=("https://www.google.com/generate_204", "https://cloudflare.com/cdn-cgi/trace")) -> dict:
    for url in urls:
        t = time.perf_counter()
        try:
            requests.head(url, timeout=4)
            return {"internet": "working", "latency_ms": round((time.perf_counter() - t) * 1000)}
        except requests.RequestException:
            continue
    return {"internet": "not reachable"}


def newest_files(folder: Path, limit: int = 10) -> list[Path]:
    try:
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(("~$", "."))
                 and p.suffix.lower() not in (".crdownload", ".part", ".tmp", ".ini")]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def old_files(folder: Path, days: int) -> list[Path]:
    """Files (not folders) directly in `folder`, last changed more than `days` days ago; never partial downloads."""
    cutoff = time.time() - days * 86400
    try:
        return sorted((p for p in folder.iterdir() if p.is_file() and p.stat().st_mtime < cutoff
                       and p.suffix.lower() not in (".crdownload", ".part", ".ini")), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []


def _age(ts: float) -> str:
    s = time.time() - ts
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return datetime.fromtimestamp(ts).strftime("%d %b")


def normalize_url(url: str) -> str | None:
    u = url.strip().strip("<>\"'")
    if not u or any(c.isspace() for c in u):
        return None
    if "://" not in u:
        if "." not in u:
            return None
        u = "https://" + u
    return u if u.lower().startswith(("http://", "https://")) else None


def register(reg: ToolRegistry) -> None:
    @reg.tool("network_status", "Is the internet working? Wi-Fi name and signal, this PC's local IP address, "
              "and how fast the internet answers (latency).", {}, group="system", read_only=True)
    def network_status():
        info: dict = {}
        try:
            r = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW, errors="replace")
            info |= wifi_info(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        if info.get("wifi_state", "").lower() != "connected":  # on a cable: "Wi-Fi disconnected" isn't an outage
            info = {"connection": "not on Wi-Fi (wired or other)"}
        info["local_ip"] = local_ip() or "none (not connected to a network)"
        info |= internet_check()
        return info

    @reg.tool("recent_files", "Only for the NEWEST files by time ('what did I just download', 'the file I was "
              "working on'): where = downloads (default), desktop, documents, or recent (opened lately in any app). "
              "To find a file by its name, use search_files.",
              {"where": {"type": "string", "enum": ["downloads", "desktop", "documents", "recent"]},
               "limit": {"type": "integer"}},
              required=[], group="files", read_only=True)
    @on_uia_thread  # resolving shortcuts uses COM
    def recent_files(where: str = "downloads", limit: int = 8):
        from jarvis.tools.file_tools import resolve_root

        limit = max(1, min(int(limit or 8), 25))
        if where == "recent":
            folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
            import win32com.client

            shell = win32com.client.Dispatch("WScript.Shell")
            rows, seen = [], set()
            for lnk in newest_files(folder, 60):
                if lnk.suffix.lower() != ".lnk":
                    continue
                try:
                    target = shell.CreateShortcut(str(lnk)).TargetPath
                except Exception:
                    continue
                if target and target not in seen and Path(target).is_file():
                    seen.add(target)
                    rows.append(f"{target}  ({_age(lnk.stat().st_mtime)})")
                if len(rows) >= limit:
                    break
            return rows or "No recently opened files found."
        folder = resolve_root(where if where in ("downloads", "desktop", "documents") else "downloads")
        files = newest_files(folder, limit)
        return [f"{p}  ({_age(p.stat().st_mtime)})" for p in files] or f"No files in {folder}."

    @reg.tool("clean_old_downloads", "Move files in the Downloads folder older than N days (default 30) to the "
              "Recycle Bin. Shows what it will move and asks the user first; folders are left alone.",
              {"older_than_days": {"type": "integer"}}, required=[], group="files")
    def clean_old_downloads(older_than_days: int = 30):
        from send2trash import send2trash

        from jarvis.tools.file_tools import resolve_root

        days = max(1, int(older_than_days or 30))
        folder = resolve_root("downloads")
        old = old_files(folder, days)
        if not old:
            return f"Nothing to clean: no files in {folder} older than {days} days."
        size = sum(p.stat().st_size for p in old)
        sample = ", ".join(p.name for p in old[:3]) + (f" and {len(old) - 3} more" if len(old) > 3 else "")
        if not reg.ask(f"move {len(old)} files ({size / 2**20:,.0f} MB) older than {days} days from Downloads to the "
                       f"Recycle Bin: {sample}"):
            return "Not cleaned: you didn't approve it. Nothing was moved."
        moved, failed = 0, 0
        for p in old:
            try:
                send2trash(str(p))
                moved += 1
            except OSError:
                failed += 1  # open in another app
        left = len(old_files(folder, days))
        text = f"Moved {moved} old download(s) ({size / 2**20:,.0f} MB) to the Recycle Bin (checked: {left} left)."
        return text + (f" {failed} couldn't be moved (in use)." if failed else "") + " Restore them from the Bin."

    @reg.tool("open_in_browser", "Just SHOW a web page in the user's own browser (a new tab, signed in as them): "
              "browser = chrome, edge, brave, firefox, or empty for the default. To search, click or type on a "
              "site, use browser_open instead (JARVIS can't act inside the user's browser).",
              {"url": {"type": "string", "description": "e.g. youtube.com or https://..."},
               "browser": {"type": "string"}}, required=["url"], group="browser")
    def open_in_browser(url: str, browser: str = ""):
        u = normalize_url(url)
        if not u:
            return f"ERROR: '{url}' isn't a web address (http/https)."
        exe = BROWSER_EXE.get((browser or "").lower().strip())
        try:
            if exe:  # ShellExecute: no command line is built, so nothing in the URL can run as a command
                os.startfile(exe, "open", u)
            else:
                os.startfile(u)
        except OSError as e:
            return f"ERROR: couldn't open {browser or 'the browser'}: {e}"
        return f"Opened {u} in {browser.title() if exe else 'the default browser'}."
