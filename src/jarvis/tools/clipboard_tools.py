"""Clipboard read/write and typing text into the active window."""

import ctypes
import time
from ctypes import wintypes

from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.web_tools import untrusted


def get_clipboard() -> str:
    import win32clipboard as cb

    cb.OpenClipboard()
    try:
        return cb.GetClipboardData(cb.CF_UNICODETEXT) if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT) else ""
    finally:
        cb.CloseClipboard()


def set_clipboard(text: str) -> None:
    import win32clipboard as cb

    cb.OpenClipboard()
    try:
        cb.EmptyClipboard()
        cb.SetClipboardText(text, cb.CF_UNICODETEXT)
    finally:
        cb.CloseClipboard()


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def type_unicode(text: str) -> None:
    """Type text into the focused window (works for any language/emoji)."""
    data = text.encode("utf-16-le")
    for code in (int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)):
        if code == 13:
            continue
        if code == 10:  # newline -> Enter key
            down = _INPUT(type=1, ki=_KEYBDINPUT(0x0D, 0, 0, 0, 0))
            up = _INPUT(type=1, ki=_KEYBDINPUT(0x0D, 0, 2, 0, 0))
        else:
            down = _INPUT(type=1, ki=_KEYBDINPUT(0, code, 0x0004, 0, 0))  # KEYEVENTF_UNICODE
            up = _INPUT(type=1, ki=_KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, 0))
        ctypes.windll.user32.SendInput(2, (_INPUT * 2)(down, up), ctypes.sizeof(_INPUT))
        time.sleep(0.004)


def register(reg: ToolRegistry) -> None:
    @reg.tool("read_clipboard", "Read the text currently copied to the clipboard.", {}, group="clipboard",
              read_only=True)
    def read_clipboard():
        text = get_clipboard()
        return untrusted("clipboard", text[:6000]) if text else "The clipboard has no text."

    @reg.tool("copy_to_clipboard", "Put text on the clipboard (e.g. a summary or translation).",
              {"text": {"type": "string"}}, group="clipboard")
    def copy_to_clipboard(text: str):
        set_clipboard(text)
        return "Copied to the clipboard."

    @reg.tool("type_text", "Type text into the window the user is working in (where the cursor is).",
              {"text": {"type": "string"}}, risk=Risk.CONFIRM, group="clipboard")
    def type_text(text: str):
        time.sleep(0.6)  # give focus back to the user's window
        type_unicode(text[:2000])
        return f"Typed {len(text)} characters."
