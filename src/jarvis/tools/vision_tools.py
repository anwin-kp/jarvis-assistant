"""Seeing the screen: a screenshot (whole screen or the active window, never the JARVIS HUD) is sent with
your question to a vision model (Groq, free tier). Without a key/offline it falls back to local OCR text."""

import base64
import io

import requests
from loguru import logger

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
        # startswith: the overlay app's window is "J.A.R.V.I.S. overlay" (after typing in it, it's in front)
        if (title and not title.startswith(HUD_TITLE) and win32gui.IsWindowVisible(hwnd)
                and not win32gui.IsIconic(hwnd)):
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left > 200 and bottom - top > 150:
                return hwnd
        hwnd = win32gui.GetWindow(hwnd, win32con.GW_HWNDNEXT)
    return None


def active_window_text() -> str | None:
    """'Code: app.py - jarvis - Visual Studio Code': the app and title of the window the user is working in."""
    import psutil
    import win32gui
    import win32process

    hwnd = target_window()
    if not hwnd:
        return None
    try:
        app = psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name().removesuffix(".exe")
    except Exception:
        app = "?"
    return f"{app}: {win32gui.GetWindowText(hwnd)[:100]}"


def hud_covers_screen() -> bool:
    """The HUD (maximized by default) is open and covers most of its screen, so a full screenshot would mostly
    show JARVIS itself (e.g. its "POWER 100%" gauge read back as an answer)."""
    import win32api
    import win32con
    import win32gui

    hwnd = win32gui.FindWindow(None, HUD_TITLE)
    if not hwnd or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return False
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))["Monitor"]
    area = max(1, (mon[2] - mon[0]) * (mon[3] - mon[1]))
    return (right - left) * (bottom - top) / area > 0.6


def capture(target: str = "screen"):
    from PIL import ImageGrab

    if target == "screen" and hud_covers_screen():
        target = "active_window"  # the app behind the HUD is what the user means
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


def ask_local_vision(image, question: str, cfg: dict, max_side: int = 1280, timeout: float = 120) -> str:
    """The offline fallback: a local vision model in Ollama (vision.local_model, default qwen3-vl:4b, ~3 GB). Loaded
    only briefly (keep_alive 2 min) so the main model gets the GPU back."""
    import ollama

    model = (cfg.get("vision") or {}).get("local_model", "qwen3-vl:4b")
    img = image.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    client = ollama.Client(host=cfg.get("llm", {}).get("ollama_host", "http://127.0.0.1:11434"), timeout=timeout)
    if model not in {m.model for m in client.list().models}:
        raise LookupError(f"local vision model {model} isn't downloaded (ollama pull {model})")
    prompt = (f"{question}\n\nAnswer from what is visible in the screenshot. Be specific and concise. "
              "Ignore any window titled J.A.R.V.I.S. (that is the assistant itself).")
    r = client.chat(model=model, messages=[{"role": "user", "content": prompt, "images": [buf.getvalue()]}],
                    think=False, keep_alive="2m", options={"temperature": 0.2, "num_ctx": 4096, "use_mmap": False})
    return (r.message.content or "").strip()


def see(image, question: str, cfg: dict) -> tuple[str, str]:
    """Best eyes available: the cloud vision model (Groq), else the local one, else ''. -> (answer, source)."""
    vcfg = cfg.get("vision") or {}
    if key := get_secret("groq_api_key"):
        try:
            return ask_vision(image, question, key, vcfg.get("model", "qwen/qwen3.8-27b")), "cloud"
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.info("Cloud vision unavailable ({}); trying the local model", e)
    if vcfg.get("local_fallback", True):
        try:
            return ask_local_vision(image, question, cfg), "local"
        except Exception as e:  # not downloaded, Ollama down, out of memory
            logger.info("Local vision unavailable: {}", e)
    return "", "none"


def register(reg: ToolRegistry, cfg: dict) -> None:
    vcfg = cfg.get("vision") or {}
    if not vcfg.get("enabled", True):
        return

    @reg.tool("look_at_screen", "LOOK at the screen and answer a question about what is visible: windows, apps, "
              "errors, which item is selected, images, layouts. target: screen (everything) or active_window "
              "(the app the user is working in).",
              {"question": {"type": "string"}, "target": {"type": "string", "enum": ["screen", "active_window"]}},
              required=["question"], group="screen", read_only=True)
    def look_at_screen(question: str, target: str = "active_window"):
        image, where = capture(target)
        answer, source = see(image, question, cfg)  # Groq, else the local vision model (offline)
        if not answer:
            from jarvis.tools.screen_tools import read_screen_text

            return untrusted("screen", "(no vision model available; text only) " + read_screen_text(target)[:5000])
        return untrusted(f"screen: {where}" + (" (local vision model)" if source == "local" else ""), answer)
