"""PC control: strict audio-device matching, winget tables, workspaces, selected text (mocked: no real keys)."""

import json

import pytest

from jarvis.tools import pc_control
from jarvis.tools.pc_control import match_device, parse_table
from jarvis.tools.registry import ToolRegistry

DEVICES = ["XV272U X1 (NVIDIA High Definition Audio)", "Speaker (Realtek(R) Audio)",
           "Headset Earphone (Realtek(R) Audio)"]


@pytest.mark.parametrize("said,want", [("speakers", 1), ("the speakers", 1), ("headphones", 2), ("headset", 2),
                                       ("monitor", 0), ("xv272u", 0), ("x", None), ("xv", None), ("realtek", None),
                                       ("audio", None), ("", None)])
def test_audio_device_matching_never_guesses(said, want):
    # a substring match once switched the real output to the monitor for the name "x"
    assert match_device(said, DEVICES) == want


WINGET = """Name               Id                 Version  Available Source
--------------------------------------------------------------------
Git                Git.Git            2.55.0.5 2.56.1    winget
GitHub CLI         GitHub.cli         2.103.3  2.105.0   winget
2 upgrades available.
"""


def test_winget_tables_are_parsed():
    rows = parse_table(WINGET)
    assert [(r["id"], r["available"]) for r in rows] == [("Git.Git", "2.56.1"), ("GitHub.cli", "2.105.0")]
    assert parse_table("No installed package found matching input criteria.") == []


def test_installs_ask_and_reject_odd_ids(tmp_path):
    reg = ToolRegistry()
    pc_control.register(reg, {}, tmp_path)
    assert reg.get("install_app").risk.value == "danger" and reg.get("update_apps").risk.value == "danger"
    assert reg.get("install_app").func("vlc & del *").startswith("ERROR")


def test_workspaces_save_and_start(tmp_path):
    reg, opened = ToolRegistry(), []

    @reg.tool("open_app", "", {"name": {"type": "string"}})
    def open_app(name):
        opened.append(name)
        return f"Opened {name} (checked: its window is open and in front)."

    @reg.tool("arrange_windows", "", {"apps": {"type": "string"}, "layout": {"type": "string"}})
    def arrange_windows(apps, layout="side_by_side"):
        return f"Arranged {apps} {layout}."

    pc_control.register(reg, {"security": {"app_allowlist": {"vs code": "code", "chrome": "chrome.exe"}}}, tmp_path)
    assert reg.get("save_workspace").func("coding", "vs code, notepad").startswith("ERROR")  # not allowed
    assert reg.get("save_workspace").func("coding", "vs code, chrome").startswith("Saved")
    out = reg.get("start_workspace").func("coding mode")
    assert opened == ["vs code", "chrome"] and "Arranged vs code, chrome side_by_side." in out
    assert json.loads((tmp_path / "workspaces.json").read_text())["coding"]["apps"] == ["vs code", "chrome"]


def test_selected_text_is_only_replaced_where_it_was_read(monkeypatch):
    clip = {"text": "mine", "seq": 1}
    keys = []

    def press(k):
        keys.append(k)
        if k == "c" and state["selected"]:  # the app copies its selection
            clip.update(text=state["selected"], seq=clip["seq"] + 1)

    state = {"selected": ""}
    monkeypatch.setattr(pc_control, "_press_ctrl", press)
    monkeypatch.setattr(pc_control, "_clip_seq", lambda: clip["seq"])
    monkeypatch.setattr(pc_control, "_focus", lambda h: True)
    monkeypatch.setattr("jarvis.tools.vision_tools.target_window", lambda: 42)
    monkeypatch.setattr("win32gui.GetWindowText", lambda h: "Notes - Notepad")
    monkeypatch.setattr("jarvis.tools.clipboard_tools.get_clipboard", lambda: clip["text"])
    monkeypatch.setattr("jarvis.tools.clipboard_tools.set_clipboard", lambda t: clip.update(text=t))
    monkeypatch.setattr(pc_control.time, "sleep", lambda s: None)
    pc_control.LAST_SELECTION.update(hwnd=None, at=0.0)

    assert pc_control.paste_text("world").startswith("ERROR")  # no selection read: never paste blind
    assert keys == []
    assert pc_control.copy_selection(timeout=0.01) is None  # nothing selected
    state["selected"] = "wrold"
    assert pc_control.copy_selection(timeout=0.01) == "wrold" and clip["text"] == "mine"  # clipboard restored
    out = pc_control.paste_text("world")
    assert out.startswith("Replaced the selection in 'Notes - Notepad'") and keys[-1] == "v"
    assert clip["text"] == "mine" and pc_control.paste_text("again").startswith("ERROR")  # one replace per read
