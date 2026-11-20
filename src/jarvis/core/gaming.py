"""Gaming mode: free the GPU while a game runs. The LLM then runs on the CPU (slower) until the game exits."""

import threading

import psutil
from loguru import logger


class GamingMode:
    def __init__(self, brain, game_processes: list[str], auto: bool = True, on_change=None):
        self.brain = brain
        self.games = {g.lower() for g in game_processes}
        self.auto = auto
        self.on_change = on_change or (lambda active, reason: None)
        self.active = False
        self.manual = False  # turned on by the user, not by a detected game
        self._stop = threading.Event()

    def set(self, active: bool, reason: str = "manual") -> None:
        if active == self.active:
            return
        self.active = active
        self.brain.set_cpu_only(active)
        if active:
            try:
                self.brain.unload()
            except Exception:
                logger.exception("Unloading model failed")
        logger.info("Gaming mode {} ({})", "on" if active else "off", reason)
        self.on_change(active, reason)

    def running_game(self) -> str | None:
        for p in psutil.process_iter(["name"]):
            name = (p.info["name"] or "").lower()
            if name in self.games:
                return name
        return None

    def start_watcher(self, interval: float = 10) -> None:
        if not self.auto or not self.games:
            return

        def loop():
            while not self._stop.wait(interval):
                game = self.running_game()
                if game and not self.active:
                    self.set(True, f"{game} started")
                elif not game and self.active and not self.manual:
                    self.set(False, "game closed")

        threading.Thread(target=loop, daemon=True, name="gaming-watcher").start()

    def stop(self) -> None:
        self._stop.set()


def register(reg, gaming: GamingMode) -> None:
    @reg.tool(
        "set_gaming_mode",
        "Turn gaming mode on or off. On = free the GPU for games (I answer more slowly using the CPU).",
        {"on": {"type": "boolean"}},
        group="system",
    )
    def set_gaming_mode(on: bool):
        gaming.manual = bool(on)
        gaming.set(bool(on))
        return "Gaming mode on: GPU freed." if on else "Gaming mode off: using the GPU again."
