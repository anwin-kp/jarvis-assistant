"""System-tray icon with quick controls."""

import threading

from PIL import Image, ImageDraw

COLORS = {"idle": (0, 170, 255), "listening": (0, 220, 120), "thinking": (255, 190, 0), "muted": (130, 130, 130)}


def _icon(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), outline=color, width=6)  # "arc reactor"
    d.ellipse((22, 22, 42, 42), fill=color)
    return img


class Tray:
    def __init__(self, actions: dict):
        """actions: callables for 'toggle_wake', 'toggle_speech', 'gaming', 'free_gpu', 'quit',
        and 'state' -> dict(wake=bool, speech=bool, gaming=bool)."""
        import pystray

        self.a = actions
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Hands-free listening", lambda: self.a["toggle_wake"](),
                 checked=lambda _: self.a["state"]()["wake"]),
            item("Speak replies", lambda: self.a["toggle_speech"](), checked=lambda _: self.a["state"]()["speech"]),
            item("Gaming mode", lambda: self.a["gaming"](), checked=lambda _: self.a["state"]()["gaming"]),
            item("Open HUD", lambda: self.a["open_hud"](), default=True),
            item("Overlay", pystray.Menu(  # desktop overlay app: the same actions as its hotkeys
                item("Show / hide  (Ctrl+Alt+H)", lambda: self.a["overlay"]("toggle-hidden")),
                item("Use it  (Ctrl+Alt+J)", lambda: self.a["overlay"]("activate")),
                item("Move to next display  (Ctrl+Alt+D)", lambda: self.a["overlay"]("next-display")),
                item("Position && size...", lambda: self.a["overlay"]("settings")),
            ), visible=lambda _: "overlay" in self.a),
            item("Free GPU memory", lambda: self.a["free_gpu"]()),
            pystray.Menu.SEPARATOR,
            item("Quit JARVIS", lambda: self.a["quit"]()),
        )
        self.icon = pystray.Icon("jarvis", _icon(COLORS["idle"]), "JARVIS", menu)

    def start(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def set_state(self, state: str) -> None:
        try:
            self.icon.icon = _icon(COLORS.get(state, COLORS["idle"]))
            self.icon.title = f"JARVIS - {state}"
        except Exception:
            pass

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "JARVIS")
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
