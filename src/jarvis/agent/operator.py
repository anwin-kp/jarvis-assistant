"""App operator: carries out a multi-step task in ANY app, like a person would.

Loop (max N steps):
  observe  - screenshot of the app window; clickable TARGETS = Windows accessibility items (where the app exposes
             them) + every OCR text line with its exact box; each target drawn as a numbered box ("set of marks")
  decide   - a vision model sees the marked screenshot + the task + what was done so far, and calls ONE action
  act      - click / double-click / right-click a target, type, press keys, scroll, wait; or finish
The model never guesses pixel coordinates (that measured ~200 px off); it picks a numbered target.
Safety: risky clicks (pay, buy, send, delete...) need approval; never types into password fields; stoppable.
"""

import asyncio
import base64
import ctypes
import io
import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from loguru import logger

GROQ = "https://api.groq.com/openai/v1/chat/completions"
RISKY = re.compile(r"\b(pay|buy|purchase|order|checkout|place order|send|post|delete|remove|erase|format|uninstall|"
                   r"confirm|submit|transfer|sign out|log ?out|unsubscribe|empty)\b", re.I)


def make_dpi_aware() -> None:
    """Screenshots, accessibility rectangles and mouse clicks must all use physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


@dataclass
class Target:
    label: str
    kind: str  # "ui" (accessibility item) or "text" (OCR line)
    rect: tuple[int, int, int, int]  # screen coords: left, top, right, bottom

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


# ---------------------------------------------------------------------------------------------- observe
async def _ocr_lines(path: Path):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    file = await StorageFile.get_file_from_path_async(str(path))
    bitmap = await (await BitmapDecoder.create_async(await file.open_async(FileAccessMode.READ))
                    ).get_software_bitmap_async()
    result = await OcrEngine.try_create_from_user_profile_languages().recognize_async(bitmap)
    out = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        left, top = min(r.x for r in rects), min(r.y for r in rects)
        right, bottom = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
        out.append((line.text, (int(left), int(top), int(right), int(bottom))))
    return out


def run_isolated(fn, *args):
    """Run WinRT (OCR) code in its own thread: WinRT and pywinauto need different COM threading modes, and a
    thread can only have one ("Cannot change thread mode after it is set")."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def ocr_targets(image, origin: tuple[int, int]) -> list[Target]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "w.png"
        image.save(path)
        lines = run_isolated(lambda: asyncio.run(_ocr_lines(path)))
    ox, oy = origin
    return [Target(text[:60], "text", (x1 + ox, y1 + oy, x2 + ox, y2 + oy))
            for text, (x1, y1, x2, y2) in lines if text.strip()]


SHORT = {"Button": "btn", "SplitButton": "btn", "ListItem": "item", "DataItem": "item", "TreeItem": "item",
         "ComboBox": "combo", "Edit": "field", "Document": "doc", "MenuItem": "menu", "Hyperlink": "link",
         "CheckBox": "check", "RadioButton": "radio", "TabItem": "tab", "Text": "text", "Slider": "slider"}


def shorten(control_type: str, name: str) -> str:
    """Compact target label: the listing is sent every step and tokens are scarce (8000/min free)."""
    return f"{SHORT.get(control_type, control_type.lower())}: {' '.join(name.split())[:40]}"


def dedupe(targets: list[Target], frame: tuple[int, int, int, int] | None = None) -> list[Target]:
    """Drop items outside the window and repeats of the same box (apps often expose a row twice)."""
    out: list[Target] = []
    for t in targets:
        cx, cy = t.center
        if frame and not (frame[0] <= cx <= frame[2] and frame[1] <= cy <= frame[3]):
            continue
        if any(all(abs(a - b) <= 6 for a, b in zip(t.rect, o.rect, strict=True)) for o in out):
            continue
        out.append(t)
    return out


def state_of(el) -> str:
    """A control's current value/state, so the model can read settings without clicking them open:
    ' = 200% (Recommended)' for a dropdown or field, ' [on]'/' [off]' for a toggle. Never a password field."""
    info = el.element_info
    try:
        if info.control_type in ("Edit", "ComboBox"):
            if getattr(info.element, "CurrentIsPassword", False):
                return ""
            value = ""
            try:
                value = el.iface_value.CurrentValue
            except Exception:
                pass
            if not value and info.control_type == "ComboBox":
                kids = el.children()
                value = kids[0].element_info.name if kids else ""
            return f" = {' '.join(str(value).split())[:40]}" if value else ""
        if info.control_type in ("Button", "CheckBox"):
            return {0: " [off]", 1: " [on]"}.get(el.iface_toggle.CurrentToggleState, "")
    except Exception:
        pass
    return ""


def ui_targets(win) -> list[Target]:
    from jarvis.tools.ui_tools import elements

    out = []
    for el in elements(win):
        r = el.rectangle()
        if r.width() > 4 and r.height() > 4:
            out.append(Target(shorten(el.element_info.control_type, el.element_info.name) + state_of(el), "ui",
                              (r.left, r.top, r.right, r.bottom)))
    w = win.rectangle()
    return dedupe(out, (w.left, w.top, w.right, w.bottom))


def merge(ui: list[Target], text: list[Target]) -> list[Target]:
    """Accessibility items first; OCR lines only where no accessibility item already covers that spot."""
    def inside(p, rect):
        return rect[0] <= p[0] <= rect[2] and rect[1] <= p[1] <= rect[3]

    extra = [t for t in text if not any(inside(t.center, u.rect) for u in ui)]
    return (ui + extra)[:150]


def annotate(image, targets: list[Target], origin: tuple[int, int]):
    from PIL import ImageDraw

    im = image.convert("RGB")
    d = ImageDraw.Draw(im)
    ox, oy = origin
    for i, t in enumerate(targets):
        left, top, right, bottom = t.rect[0] - ox, t.rect[1] - oy, t.rect[2] - ox, t.rect[3] - oy
        color = (255, 40, 40) if t.kind == "ui" else (30, 120, 255)
        d.rectangle([left, top, right, bottom], outline=color, width=2)
        d.rectangle([left, top - 12, left + 8 * len(str(i)) + 4, top], fill=color)
        d.text((left + 2, top - 12), str(i), fill=(255, 255, 255))
    return im


# ---------------------------------------------------------------------------------------------- act
KEYMAP = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}
NAMED = {"enter": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}", "space": "{SPACE}",
         "backspace": "{BACKSPACE}", "delete": "{DELETE}", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}",
         "right": "{RIGHT}", "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
         "f5": "{F5}", "f11": "{F11}", "win": "{VK_LWIN}"}


def to_send_keys(combo: str) -> str:
    """'ctrl+shift+s' -> '^+s', 'enter' -> '{ENTER}', 'alt+f4' -> '%{F4}'."""
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    mods = "".join(KEYMAP[p] for p in parts[:-1] if p in KEYMAP)
    key = parts[-1] if parts else ""
    if key in NAMED:
        key = NAMED[key]
    elif re.fullmatch(r"f\d{1,2}", key):
        key = "{" + key.upper() + "}"
    return mods + key


class Operator:
    def __init__(self, api_key: str, model: str = "qwen/qwen3.8-27b", max_steps: int = 15, confirm=None,
                 cancel: threading.Event | None = None, http=None, text_model: str = "openai/gpt-oss-120b"):
        # steps without a new screenshot go to a text model: separate free quota, so tasks wait less
        self.key, self.model, self.text_model, self.max_steps = api_key, model, text_model, max_steps
        self.confirm = confirm or (lambda what: False)
        self.cancel = cancel or threading.Event()
        self.http = http or requests

    LOOK = {"type": "function", "function": {
        "name": "look", "description": "See a screenshot of the window, when the target list isn't enough "
        "(unnamed icons, images, layout) or to check the result.", "parameters": {"type": "object", "properties": {}}}}
    TOOLS = [
        {"type": "function", "function": {"name": "click", "description": "Click a numbered target.",
         "parameters": {"type": "object", "properties": {
             "target": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"]},
             "double": {"type": "boolean"}}, "required": ["target"]}}},
        {"type": "function", "function": {"name": "type_text", "description": "Type text (optionally click a target "
         "first to focus it), optionally press Enter after.",
         "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "target": {"type": "integer"},
                                                        "enter": {"type": "boolean"}}, "required": ["text"]}}},
        {"type": "function", "function": {"name": "press_keys", "description": "Press a key or shortcut, e.g. "
         "'enter', 'ctrl+f', 'ctrl+l', 'alt+left', 'escape', 'down'.",
         "parameters": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}}},
        {"type": "function", "function": {"name": "scroll", "description": "Scroll the window (or over a target).",
         "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                                                        "target": {"type": "integer"}}, "required": ["direction"]}}},
        {"type": "function", "function": {"name": "wait", "description": "Wait for the app to load/update.",
         "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}}},
        {"type": "function", "function": {"name": "done", "description": "The task is complete (or impossible).",
         "parameters": {"type": "object", "properties": {"success": {"type": "boolean"},
                                                        "summary": {"type": "string"}},
                        "required": ["success", "summary"]}}},
    ]

    def run(self, task: str, hwnd: int) -> str:
        from PIL import ImageGrab
        from pywinauto import Desktop

        make_dpi_aware()
        win = Desktop(backend="uia").window(handle=hwnd)
        history: list[str] = []
        before: list[str] | None = None  # target labels before the last action
        actions: list[str] = []  # what each step did, to notice repeats
        for step in range(1, self.max_steps + 1):
            if self.cancel.is_set():
                return "Stopped."
            try:
                win.set_focus()
            except Exception:
                pass
            time.sleep(0.3)
            rect = win.rectangle()
            origin = (rect.left, rect.top)
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
            targets = merge(ui_targets(win), ocr_targets(image, origin))
            labels = [t.label for t in targets]
            if history and labels == before:
                history[-1] += " (nothing changed on screen)"
            if len(actions) >= 4 and len(set(actions[-4:])) == 1:
                return "I got stuck in that app (the same step kept doing nothing). " + "; ".join(history[-3:])
            if len(actions) >= 2 and actions[-1] == actions[-2]:
                history.append("WARNING: you repeated the same step; it isn't working. Do something different, "
                               "or call done with what you can already see.")
            before = labels
            # Text first: the target list usually says enough, and a screenshot step costs ~2.4x the tokens
            # (8000/min free quota). The text model asks to "look" when it needs to see; apps that expose
            # little text (games, VS Code) always get the screenshot.
            try:
                call = None
                if self.text_model and len(targets) >= 15:
                    call = self._decide(task, history, None, targets)
                if call is None or call[0] == "look":
                    call = self._decide(task, history, annotate(image, targets, origin), targets)
            except RuntimeError:
                return "Stopped."
            if call is None:
                return "I couldn't decide what to do next in that app."
            name, args = call
            logger.info("Operator step {}: {} {}", step, name, args)
            if name == "done":
                return args.get("summary", "Done.") if args.get("success", True) else \
                    "I couldn't finish: " + args.get("summary", "")
            result = self._act(name, args, targets, win)
            actions.append(f"{name} {result}")
            history.append(f"{step}. {name} {json.dumps(args)} -> {result}")
            if result.startswith("STOP"):
                return result[5:].strip()
        return "I ran out of steps before finishing. Here's what I did: " + "; ".join(history[-4:])

    def _post(self, body: dict) -> dict:
        """POST to Groq, waiting out the free tier's per-minute token limit (429) instead of failing."""
        waited = 0.0
        while True:
            r = self.http.post(GROQ, timeout=60, headers={"Authorization": f"Bearer {self.key}"}, json=body)
            if getattr(r, "status_code", 200) != 429 or waited > 90:
                if getattr(r, "status_code", 200) >= 400:
                    logger.warning("Operator: Groq {} {}", r.status_code, getattr(r, "text", "")[:300])
                r.raise_for_status()
                return r.json()
            delay = min(30.0, float(r.headers.get("retry-after") or 5) + 0.5)
            logger.info("Operator: rate limited, waiting {:.0f}s", delay)
            if self.cancel.wait(delay):
                raise RuntimeError("cancelled")
            waited += delay

    def _decide(self, task, history, marked, targets):
        listing = "\n".join(f"[{i}] {t.label}" for i, t in enumerate(targets))
        prompt = (f"You are operating a Windows app for the user. TASK: {task}\n\n"
                  f"Done so far:\n{chr(10).join(history) or '(nothing yet)'}\n\n"
                  "Numbered targets (with a type prefix = app controls, without = text read from the screen):\n"
                  f"{listing}\n\n"
                  + ("Look at the screenshot (numbered boxes: red = controls, blue = text) and call"
                     if marked is not None else "This list is everything readable on screen now. If it isn't "
                     "enough to tell what to do or whether the task is done, call look to see a screenshot. "
                     "Otherwise call")
                  + " exactly ONE tool for the next step. Prefer "
                  "clicking targets by number; use keyboard shortcuts when quicker. When the task is complete, "
                  "call done. If the task asks for information, answer it in done's summary as soon as the "
                  "target list or screenshot shows it (a control's current value is shown after '=' and on/off "
                  "states in [brackets]). If something needs the user's payment details or password, call done with "
                  "success=false and say so.")
        content = [{"type": "text", "text": prompt}]
        if marked is not None:
            img = marked.convert("RGB")
            img.thumbnail((1600, 1600))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {"model": self.model, "temperature": 0.1, "max_tokens": 300,
                "tools": self.TOOLS if marked is not None else self.TOOLS + [self.LOOK],
                "messages": [{"role": "user", "content": content}]}
        if marked is None and self.text_model:
            body.update(model=self.text_model, max_tokens=600, reasoning_effort="low")
            body["messages"][0]["content"] = prompt
            try:
                data = self._post(body)
            except requests.HTTPError:
                return None  # e.g. 400 tool_use_failed (malformed tool call): the vision model decides this step
        else:
            data = self._post(body)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            return None
        fn = calls[0]["function"]
        try:
            return fn["name"], json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return None

    def _act(self, name: str, args: dict, targets: list[Target], win) -> str:
        def target(i):
            if i is None or not (0 <= int(i) < len(targets)):
                return None
            return targets[int(i)]

        if name == "click":
            t = target(args.get("target"))
            if t is None:
                return "no such target"
            if RISKY.search(t.label) and not self.confirm(f"click '{t.label}'"):
                return "STOP I stopped before clicking '" + t.label + "' because you didn't approve it."
            from pywinauto import mouse

            btn = args.get("button", "left")
            if args.get("double"):
                mouse.double_click(button=btn, coords=t.center)
            else:
                mouse.click(button=btn, coords=t.center)
            time.sleep(0.6)
            return f"clicked '{t.label}'"
        if name == "type_text":
            from pywinauto import keyboard, mouse

            t = target(args.get("target"))
            if t is not None:
                mouse.click(coords=t.center)
                time.sleep(0.2)
            if _focused_is_password():
                return "STOP I never type into password fields - please enter that yourself."
            from jarvis.tools.clipboard_tools import type_unicode

            type_unicode(args.get("text", ""))
            if args.get("enter"):
                keyboard.send_keys("{ENTER}")
            time.sleep(0.5)
            return "typed"
        if name == "press_keys":
            keys = args.get("keys", "")
            if keys.lower().replace(" ", "") in ("alt+f4", "ctrl+w", "ctrl+q") and not self.confirm(f"press {keys}"):
                return "STOP I didn't close anything because you didn't approve it."
            from pywinauto import keyboard

            keyboard.send_keys(to_send_keys(keys))
            time.sleep(0.5)
            return f"pressed {keys}"
        if name == "scroll":
            from pywinauto import mouse

            t = target(args.get("target"))
            r = win.rectangle()
            coords = t.center if t else ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            mouse.scroll(coords=coords, wheel_dist=-5 if args.get("direction") == "down" else 5)
            time.sleep(0.4)
            return f"scrolled {args.get('direction')}"
        if name == "wait":
            time.sleep(min(5.0, float(args.get("seconds", 1))))
            return "waited"
        return f"unknown action {name}"


def _focused_is_password() -> bool:
    try:
        from pywinauto.uia_defines import IUIA

        el = IUIA().iuia.GetFocusedElement()
        return bool(el.CurrentIsPassword)
    except Exception:
        return False
