"""Screen understanding with Windows' built-in OCR (free, offline): 'what's this error on my screen?'"""

import asyncio
import tempfile
from pathlib import Path

from jarvis.tools.registry import ToolRegistry
from jarvis.tools.web_tools import untrusted

HUD_TITLE = "J.A.R.V.I.S."


async def ocr_file(path: Path) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    file = await StorageFile.get_file_from_path_async(str(path))
    stream = await file.open_async(FileAccessMode.READ)
    bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No OCR language installed (Settings > Time & language > Language)")
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def grab(target: str):
    """Screenshot of the whole screen or the active window (never the JARVIS HUD itself)."""
    from PIL import ImageGrab

    if target == "active_window":
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if hwnd and win32gui.GetWindowText(hwnd) != HUD_TITLE:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left > 50 and bottom - top > 50:
                return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    return ImageGrab.grab(all_screens=True)


def read_screen_text(target: str = "screen") -> str:
    img = grab(target)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "screen.png"
        img.save(path)
        return asyncio.run(ocr_file(path))


def register(reg: ToolRegistry) -> None:
    @reg.tool("read_screen", "Read the text currently visible on the screen (or only the active window), "
              "e.g. to explain an error message or summarize what the user is looking at.",
              {"target": {"type": "string", "enum": ["screen", "active_window"]}}, required=[],
              group="screen", read_only=True)
    def read_screen(target: str = "screen"):
        text = read_screen_text(target).strip()
        return untrusted("screen", text[:6000]) if text else "No readable text on the screen."
