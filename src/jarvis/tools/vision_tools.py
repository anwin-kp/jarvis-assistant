"""Seeing the screen: a screenshot (whole screen or the active window, never the JARVIS HUD) is sent with
your question to a vision model (Groq, free tier). Without a key/offline it falls back to local OCR text."""

import base64
import io

import requests

from jarvis.core.config import get_secret
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.web_tools import untrusted

HUD_TITLE = "J.A.R.V.I.S."


def target_window():
    """The window the user is working in: the foreground one, or the next one behind the HUD."""
    import win32con
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    while hwnd:
        title = win32gui.GetWindowText(hwnd)
        if (title and title != HUD_TITLE and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd)):
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left > 200 and bottom - top > 150:
                return hwnd
        hwnd = win32gui.GetWindow(hwnd, win32con.GW_HWNDNEXT)
    return None


def capture(target: str = "screen"):
    from PIL import ImageGrab

    if target == "active_window":
        import win32gui

        hwnd = target_window()
        if hwnd:
            return ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd), all_screens=True), win32gui.GetWindowText(hwnd)
    return ImageGrab.grab(all_screens=True), "whole screen"


def ask_vision(image, question: str, key: str, model: str, max_side: int = 1600, timeout: float = 45) -> str:
    img = image.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    prompt = (f"{question}\n\nAnswer from what is visible in the screenshot. Be specific and concise. "
              "Ignore any window titled J.A.R.V.I.S. (that is the assistant itself).")
    r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=timeout,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_tokens": 600, "temperature": 0.2, "messages": [{
                          "role": "user", "content": [{"type": "text", "text": prompt},
                                                      {"type": "image_url",
                                                       "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def register(reg: ToolRegistry, cfg: dict) -> None:
    vcfg = cfg.get("vision") or {}
    if not vcfg.get("enabled", True):
        return
    model = vcfg.get("model", "qwen/qwen3.8-27b")

    @reg.tool("look_at_screen", "LOOK at the screen and answer a question about what is visible: windows, apps, "
              "errors, which item is selected, images, layouts. target: screen (everything) or active_window "
              "(the app the user is working in).",
              {"question": {"type": "string"}, "target": {"type": "string", "enum": ["screen", "active_window"]}},
              required=["question"], group="screen", read_only=True)
    def look_at_screen(question: str, target: str = "active_window"):
        image, where = capture(target)
        key = get_secret("groq_api_key")
        if not key:
            from jarvis.tools.screen_tools import read_screen_text

            return untrusted("screen", "(no vision model; text only) " + read_screen_text(target)[:5000])
        try:
            answer = ask_vision(image, question, key, model)
        except requests.RequestException as e:
            from jarvis.tools.screen_tools import read_screen_text

            return untrusted("screen", f"(vision unavailable: {e}; text only) " + read_screen_text(target)[:5000])
        return untrusted(f"screen: {where}", answer)
