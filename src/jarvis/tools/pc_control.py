"""Whole-PC control, part 1: audio devices, installing / updating apps (winget), workspaces, and the selected text in
any app. Every action is checked after it runs; installs and updates always ask first and run in the background."""

import ctypes
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

from jarvis.agent.uia import on_uia_thread
from jarvis.tools.registry import Risk, ToolRegistry

# ------------------------------------------------------------------ audio devices

RENDER, CAPTURE, ACTIVE = 0, 1, 1


def _devices(flow: int) -> list:
    from pycaw.pycaw import AudioUtilities

    return [d for d in AudioUtilities.GetAllDevices(data_flow=flow, device_state=ACTIVE) if d.FriendlyName]


def _default_id(flow: int) -> str | None:
    from pycaw.pycaw import AudioUtilities

    d = AudioUtilities.GetSpeakers() if flow == RENDER else AudioUtilities.GetMicrophone()
    return getattr(d, "id", None) or (d.GetId() if hasattr(d, "GetId") else None)


SYNONYMS = {"headphones": "headset", "headphone": "headset", "earphones": "headset", "earbuds": "headset",
            "buds": "headset", "speakers": "speaker", "monitor": "display", "screen": "display", "tv": "display"}
KINDS = {"headset": ("headset", "headphone", "earphone", "earbud", "buds", "hands-free", "airpods"),
         "speaker": ("speaker",), "display": ("hdmi", "display", "nvidia high definition", "monitor")}


def match_device(name: str, names: list[str]) -> int | None:
    """'headset' -> 'Headset Earphone (Realtek(R) Audio)': the ONE device whose name has every word you said (whole
    words, or word starts of 3+ letters), or a device of the kind you said. Vague -> None: never guess. (A plain
    substring match once took "x" for the monitor "XV272U" and really switched the output.)"""
    said = [SYNONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", name.lower())]
    said = [w for w in said if w not in ("the", "my", "to", "device", "audio", "output", "input", "default")]
    if not said:
        return None
    words = [re.findall(r"[a-z0-9]+", n.lower()) for n in names]

    def has(w: str, ws: list[str]) -> bool:
        return w in ws or (len(w) >= 3 and any(x.startswith(w) for x in ws))

    exact = [i for i, n in enumerate(names) if n.lower() == name.lower().strip()]
    by_words = [i for i, ws in enumerate(words) if all(has(w, ws) for w in said)]
    by_kind = [i for i, n in enumerate(names) if len(said) == 1 and said[0] in KINDS
               and any(k in n.lower() for k in KINDS[said[0]])]
    for hits in (exact, by_words, by_kind):
        if len(hits) == 1:
            return hits[0]
    return None


# ------------------------------------------------------------------ winget

WINGET_FLAGS = ["--accept-source-agreements", "--disable-interactivity"]


def parse_table(out: str) -> list[dict]:
    """winget's fixed-width table (Name  Id  Version  [Available]  Source) -> rows."""
    lines = [ln.rstrip() for ln in out.splitlines()]
    head = next((i for i, ln in enumerate(lines) if re.match(r"^Name\s+Id\s+Version", ln)), None)
    if head is None:
        return []
    cols = [(m.group(0).strip().lower(), m.start()) for m in re.finditer(r"\S+\s*", lines[head])]
    rows = []
    for ln in lines[head + 2:]:
        if not ln.strip() or re.match(r"^\d+ (upgrades?|packages?) ", ln):
            continue
        row = {}
        for i, (name, start) in enumerate(cols):
            end = cols[i + 1][1] if i + 1 < len(cols) else None
            row[name] = ln[start:end].strip()
        if row.get("id"):
            rows.append(row)
    return rows


def winget(*args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(["winget", *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    except FileNotFoundError:
        return 1, "winget isn't available on this PC (it comes with the 'App Installer' from the Microsoft Store)."
    except subprocess.TimeoutExpired:
        return 1, f"winget took longer than {timeout // 60} minutes."
    # progress bars and spinners: keep the readable lines
    text = "\n".join(ln for ln in (r.stdout or "").replace("\r", "\n").splitlines()
                     if ln.strip() and not re.fullmatch(r"[\s\-\\|/█▒]+.*%?|\s*[\-\\|/]\s*", ln))
    return r.returncode, text


def background(reg: ToolRegistry, name: str, fn) -> None:
    """Long jobs (installs, updates) run off the turn; the result is spoken when done."""
    def run():
        try:
            out = fn()
        except Exception as e:  # report, never crash silently
            logger.exception("{} failed", name)
            out = f"{name} failed: {e}"
        (getattr(reg, "notify", None) or logger.info)(out)

    threading.Thread(target=run, daemon=True, name=name).start()


# ------------------------------------------------------------------ selected text

KEYEVENTF_KEYUP, VK_CONTROL = 2, 0x11


def _press_ctrl(key: str) -> None:
    vk = ord(key.upper())
    u = ctypes.windll.user32
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(vk, 0, 0, 0)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    u.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _clip_seq() -> int:
    return ctypes.windll.user32.GetClipboardSequenceNumber()


def _focus(hwnd: int) -> bool:
    import win32gui

    from jarvis.tools.window_tools import focus

    try:
        focus(hwnd)
    except Exception:
        pass
    time.sleep(0.15)
    return win32gui.GetForegroundWindow() == hwnd


# The window a selection was really copied from, and when: replace_selected_text pastes ONLY there, soon after.
# (A test once pasted into whatever window was in front - an open code editor - because nothing tied the paste to
# a real selection.)
LAST_SELECTION: dict = {"hwnd": None, "at": 0.0, "title": ""}
SELECTION_TTL = 600


def copy_selection(timeout: float = 1.0) -> str | None:
    """Ctrl+C in the user's window (never JARVIS's own); the copied text, or None if nothing was selected.
    The clipboard is restored afterwards."""
    import win32gui

    from jarvis.tools.clipboard_tools import get_clipboard, set_clipboard
    from jarvis.tools.vision_tools import target_window

    hwnd = target_window()
    if not hwnd or not _focus(hwnd):
        return None
    before = get_clipboard()
    seq = _clip_seq()
    _press_ctrl("c")
    end = time.monotonic() + timeout
    while time.monotonic() < end and _clip_seq() == seq:
        time.sleep(0.05)
    if _clip_seq() == seq:
        return None  # Ctrl+C copied nothing: nothing selected
    text = get_clipboard()
    set_clipboard(before)
    if text:
        LAST_SELECTION.update(hwnd=hwnd, at=time.monotonic(), title=win32gui.GetWindowText(hwnd)[:60])
    return text


def paste_text(text: str) -> str:
    """Paste over the selection captured by copy_selection, in that same window. Returns what happened."""
    from jarvis.tools.clipboard_tools import get_clipboard, set_clipboard

    hwnd = LAST_SELECTION["hwnd"]
    if not hwnd or time.monotonic() - LAST_SELECTION["at"] > SELECTION_TTL:
        return "ERROR: I haven't read a selection to replace. Select the text, then ask again (I read it first)."
    if not _focus(hwnd):
        return f"ERROR: couldn't bring '{LAST_SELECTION['title']}' back to the front, so nothing was pasted."
    before = get_clipboard()
    set_clipboard(text)
    _press_ctrl("v")
    time.sleep(0.4)  # let the app read the clipboard before it's restored
    set_clipboard(before)
    LAST_SELECTION.update(hwnd=None)  # one replacement per selection read
    return f"Replaced the selection in '{LAST_SELECTION['title']}' ({len(text)} characters). Ctrl+Z there undoes it."


# ------------------------------------------------------------------ registration


def register(reg: ToolRegistry, cfg: dict, data: Path) -> None:
    @reg.tool("list_audio_devices", "List the speakers/headphones (output) or microphones (input); the default is "
              "marked with *.", {"kind": {"type": "string", "enum": ["output", "input"]}}, required=[],
              group="system", read_only=True)
    @on_uia_thread  # COM, like the volume tools
    def list_audio_devices(kind: str = "output"):
        flow = CAPTURE if kind == "input" else RENDER
        default = _default_id(flow)
        return [f"{'* ' if d.id == default else ''}{d.FriendlyName}" for d in _devices(flow)] or "No devices found."

    @reg.tool("set_audio_device", "Switch the default speakers/headphones (output) or microphone (input) to another "
              "device, e.g. name='headset' or 'speakers'.",
              {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["output", "input"]}},
              required=["name"], group="system")
    @on_uia_thread
    def set_audio_device(name: str, kind: str = "output"):
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities

        flow = CAPTURE if kind == "input" else RENDER
        devs = _devices(flow)
        i = match_device(name, [d.FriendlyName for d in devs])
        if i is None:
            names = ", ".join(d.FriendlyName for d in devs)
            return f"ERROR: no single {kind} device matches '{name}' (nothing changed). Devices: {names}"
        dev = devs[i]
        AudioUtilities.SetDefaultDevice(dev.id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        if _default_id(flow) != dev.id:
            return f"WARNING: asked Windows to switch to {dev.FriendlyName}, but the default didn't change."
        return f"Switched the default {kind} to {dev.FriendlyName} (checked)."

    @reg.tool("search_apps", "Search for apps to install (winget: Windows' package manager).",
              {"query": {"type": "string"}}, group="apps", read_only=True)
    def search_apps(query: str):
        code, out = winget("search", query, "--count", "8", *WINGET_FLAGS)
        rows = parse_table(out)
        if not rows:
            return f"No apps found for '{query}'." if code == 0 or "No package" in out else f"ERROR: {out[-300:]}"
        return [f"{r.get('name')} (id {r['id']}, version {r.get('version', '?')})" for r in rows]

    @reg.tool("install_app", "Install an app with winget, by its exact id from search_apps (e.g. VideoLAN.VLC). "
              "Runs in the background; JARVIS says when it's done.",
              {"app_id": {"type": "string"}}, risk=Risk.DANGER, group="apps")
    def install_app(app_id: str):
        if not re.fullmatch(r"[\w.+-]{2,80}", app_id):
            return "ERROR: give the exact app id from search_apps (e.g. VideoLAN.VLC)."

        def job():
            code, out = winget("install", "--id", app_id, "-e", "--silent", "--accept-package-agreements",
                               *WINGET_FLAGS, timeout=1800)
            listed = winget("list", "--id", app_id, "-e", *WINGET_FLAGS)[1]
            if app_id.lower() in listed.lower():
                return f"Installed {app_id} (checked: it's in the installed apps list)."
            return f"Installing {app_id} didn't work: {out[-200:]}"

        background(reg, f"install {app_id}", job)
        return f"Installing {app_id} in the background; I'll tell you when it's done."

    @reg.tool("list_app_updates", "Which installed apps have updates available (winget).", {}, group="apps",
              read_only=True)
    def list_app_updates():
        code, out = winget("upgrade", "--include-unknown", *WINGET_FLAGS)
        rows = [r for r in parse_table(out) if r.get("available")]
        return [f"{r.get('name')} {r.get('version')} -> {r['available']} (id {r['id']})" for r in rows] or \
            "Everything is up to date."

    @reg.tool("update_apps", "Update one app (its id from list_app_updates) or all apps ('all') with winget. Runs "
              "in the background; JARVIS says when it's done.", {"app_id": {"type": "string"}},
              risk=Risk.DANGER, group="apps")
    def update_apps(app_id: str):
        target = ["--all"] if app_id.lower() in ("all", "everything", "*") else ["--id", app_id, "-e"]
        if target != ["--all"] and not re.fullmatch(r"[\w.+-]{2,80}", app_id):
            return "ERROR: give the exact app id from list_app_updates, or 'all'."

        def job():
            before = {r["id"] for r in parse_table(winget("upgrade", *WINGET_FLAGS)[1]) if r.get("available")}
            winget("upgrade", *target, "--silent", "--accept-package-agreements", *WINGET_FLAGS, timeout=3600)
            after = {r["id"] for r in parse_table(winget("upgrade", *WINGET_FLAGS)[1]) if r.get("available")}
            done = sorted(before - after) if target == ["--all"] else ([app_id] if app_id not in after else [])
            left = sorted(after & (before if target == ["--all"] else {app_id}))
            return (f"Updated {len(done)} app(s): {', '.join(done[:8]) or 'none'}"
                    + (f". Still pending: {', '.join(left[:5])}" if left else "") + " (checked).")

        background(reg, "update apps", job)
        return "Updating in the background; I'll tell you when it's done."

    # -------------------------------------------------------------- workspaces
    ws_file = data / "workspaces.json"

    def workspaces() -> dict:
        found = dict(cfg.get("workspaces") or {})
        try:
            found |= json.loads(ws_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {k.lower(): v for k, v in found.items()}

    @reg.tool("start_workspace", "Open a named set of apps and arrange them side by side, e.g. 'work mode'.",
              {"name": {"type": "string"}}, group="windows")
    def start_workspace(name: str):
        all_ = workspaces()
        key = name.lower().removesuffix(" mode").removesuffix(" workspace").strip()
        ws = all_.get(key) or all_.get(name.lower())
        if not ws:
            return f"ERROR: no workspace '{name}'. Known: {', '.join(all_) or 'none'} (save one with save_workspace)."
        opened = [reg.get("open_app").func(a) for a in ws.get("apps", [])]
        time.sleep(1.5)  # windows appear
        arranged = reg.get("arrange_windows").func(", ".join(ws["apps"]), ws.get("layout", "side_by_side")) \
            if len(ws.get("apps", [])) > 1 else ""
        bad = [o for o in opened if o.startswith(("ERROR", "Started"))]
        return f"Workspace {key}: " + "; ".join(bad or [f"opened {', '.join(ws['apps'])}"]) + \
            (f". {arranged}" if arranged else "")

    @reg.tool("save_workspace", "Save a named set of apps to open and arrange together (apps from the app list).",
              {"name": {"type": "string"}, "apps": {"type": "string", "description": "comma-separated, in order"},
               "layout": {"type": "string", "enum": ["side_by_side", "stacked"]}},
              required=["name", "apps"], group="windows")
    def save_workspace(name: str, apps: str, layout: str = "side_by_side"):
        app_list = [a.strip().lower() for a in apps.split(",") if a.strip()][:4]
        allow = {k.lower() for k in (cfg.get("security", {}).get("app_allowlist") or {})}
        unknown = [a for a in app_list if a not in allow]
        if unknown:
            return f"ERROR: not in my app list: {', '.join(unknown)}. Known apps: {', '.join(sorted(allow))}."
        saved = {}
        try:
            saved = json.loads(ws_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        saved[name.lower()] = {"apps": app_list, "layout": layout}
        ws_file.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        return f"Saved workspace '{name}': {', '.join(app_list)} ({layout.replace('_', ' ')})."

    @reg.tool("list_workspaces", "List the saved workspaces.", {}, group="windows", read_only=True)
    def list_workspaces():
        return {k: ", ".join(v.get("apps", [])) for k, v in workspaces().items()} or "No workspaces yet."

    # -------------------------------------------------------------- selected text
    @reg.tool("get_selected_text", "The text the user has selected (highlighted) in the app they're working in. "
              "Use it for 'rewrite / translate / explain / summarize this selected text'.", {},
              group="clipboard")  # not read_only: it presses Ctrl+C in the user's window (dry runs simulate it)
    def get_selected_text():
        text = copy_selection()
        return text if text else "ERROR: nothing is selected (select some text first, then ask again)."

    @reg.tool("replace_selected_text", "Replace the selected text in the user's app with new text (e.g. after "
              "rewriting it). Ctrl+Z in that app undoes it.", {"text": {"type": "string"}},
              risk=Risk.CONFIRM, group="clipboard")
    def replace_selected_text(text: str):
        return paste_text(text)
