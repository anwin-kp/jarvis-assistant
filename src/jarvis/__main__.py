"""JARVIS console: typing, push-to-talk, 'Hey Jarvis' wake word and tray icon.
Run:  .\\jarvis.ps1 start   (or: uv run python -m jarvis)"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime

from loguru import logger

from jarvis.app import build
from jarvis.core import gaming as gaming_mod
from jarvis.core.config import DATA, ensure_data_dirs, list_personas, load_persona
from jarvis.core.proactive import Proactive
from jarvis.llm.ollama_brain import BrainUnavailable
from jarvis.scheduler import reminders as reminders_mod
from jarvis.tools.registry import Risk, Tool

CYAN, YELLOW, RED, DIM, RESET = "\033[96m", "\033[93m", "\033[91m", "\033[2m", "\033[0m"

HELP = """Commands:
  (empty Enter)  talk (push-to-talk)      say "Hey Jarvis"  talk hands-free
  /help     show this help
  /reset    forget the current conversation (saved facts stay)
  /facts    list what JARVIS remembers about you
  /export   save everything JARVIS knows (facts, summaries, history) to a JSON file
  /tools    list available tools
  /unload   free the GPU memory now
  /briefing your daily briefing now
  /gaming   toggle gaming mode (GPU freed, slower CPU replies)
  /hud      open the HUD window
  /private  toggle private mode (never use the cloud model)
  /persona  switch personality, e.g. /persona butler
  /wake     toggle the wake word
  /mute     stop speaking replies (/unmute to resume)
  /quit     exit"""

STATUS_MARK = {"ok": "✓", "dry-run": "(simulated)", "declined": "✗ declined", "denied": "✗ not allowed",
               "error": "✗ failed", "invalid": "✗ invalid request"}


def show_tool_call(name: str, args: dict, status: str) -> None:
    """Print what actually ran, so the user never has to trust the model's own claims."""
    arg_text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    if len(arg_text) > 80:
        arg_text = arg_text[:77] + "..."
    print(f"{DIM}  · {name}({arg_text}) {STATUS_MARK.get(status, status)}{RESET}")


class Voice:
    """Microphone + speech recognition + speech output."""

    def __init__(self, vcfg: dict):
        from jarvis.stt.whisper_stt import WhisperSTT
        from jarvis.tts.sapi_tts import SapiTTS

        self.cfg = vcfg
        self.stt = WhisperSTT(vcfg.get("stt_model", "small"), vcfg.get("stt_device", "cuda"),
                              vcfg.get("stt_compute_type", "float16"), download_root=str(DATA / "models"))
        self.tts = None
        if vcfg.get("tts_engine", "piper") == "piper":
            try:
                from jarvis.tts.piper_tts import PiperTTS

                self.tts = PiperTTS(vcfg.get("piper_voice", "en_GB-alan-medium"), DATA / "models" / "piper",
                                    vcfg.get("tts_speed", 1.0))
            except Exception as e:
                logger.warning("Piper unavailable ({}), using Windows voice", e)
        if self.tts is None:
            self.tts = SapiTTS(vcfg.get("tts_rate", 185), vcfg.get("tts_voice"))

    def listen(self, start_timeout: float = 6) -> str:
        from jarvis.audio.mic import record_until_silence

        audio = record_until_silence(self.cfg.get("max_seconds", 15), self.cfg.get("silence_ms", 800),
                                     start_timeout=start_timeout)
        text, _ = self.stt.transcribe(audio, self.cfg.get("language"))
        return text


def setup_logging(debug: bool) -> None:
    logger.remove()
    logger.add(DATA / "logs" / "jarvis.log", rotation="5 MB", retention=5, level="DEBUG", encoding="utf-8")
    if debug:
        logger.add(sys.stderr, level="DEBUG")


class App:
    def __init__(self, args):
        self.cfg, self.orch, self.brain, self.reg, self.memory, _ = build(dry_run=args.dry_run)
        self.reg.on_call = self._on_tool
        self.hud = None
        self.name = self.cfg.get("assistant_name", "JARVIS")
        self.q: queue.Queue = queue.Queue()
        self.busy = threading.Event()  # set while a turn runs: pauses the wake-word listener
        self.quit = threading.Event()
        self.voice = self.wake = self.tray = None
        self.speech_on = False
        self.wake_on = False
        self.args = args

        gcfg = self.cfg.get("gaming", {})
        self.gaming = gaming_mod.GamingMode(self.brain, gcfg.get("processes", []), gcfg.get("auto_detect", True),
                                            on_change=self._gaming_changed)
        gaming_mod.register(self.reg, self.gaming)

        bcfg = self.cfg.get("briefing", {})
        self.reminders = reminders_mod.Reminders(
            self.memory, self._reminder_fired, bcfg.get("time") if bcfg.get("enabled", True) else None)
        reminders_mod.register(self.reg, self.reminders)

        pcfg = self.cfg.get("proactive", {})
        self.proactive = Proactive(lambda m: self.q.put(("alert", m)), pcfg.get("battery_percent", 15),
                                   pcfg.get("disk_free_gb", 10)) if pcfg.get("enabled", True) else None

    # ------------------------------------------------------------ setup
    def start(self) -> None:
        hcfg = self.cfg.get("hud", {})
        if hcfg.get("enabled", True):
            try:
                from jarvis.ui.hud import Hud

                self.hud = Hud(self.q, hcfg.get("port", 8765))
                self.hud.start()
            except Exception:
                logger.exception("HUD failed to start")
                self.hud = None
        try:
            self.brain.check()
        except BrainUnavailable as e:
            print(f"{RED}{e}{RESET}")
            sys.exit(1)
        print(f"{CYAN}{self.name} online.{RESET} {DIM}Model: {self.brain.model} · {len(self.reg.names())} tools"
              f" · /help for commands{RESET}")
        if self.args.dry_run:
            print(f"{YELLOW}Dry-run mode: actions that change things will only be simulated.{RESET}")

        vcfg = self.cfg.get("voice", {})
        if vcfg.get("enabled", True) and not self.args.text:
            print(f"{DIM}Loading voice…{RESET}", end="\r", flush=True)
            try:
                self.voice = Voice(vcfg)
                self.speech_on = True
                if vcfg.get("wake_word", True):
                    from jarvis.audio.wakeword import WakeWord

                    self.wake = WakeWord(vcfg.get("wake_threshold", 0.5))
                    self.wake_on = True
                    threading.Thread(target=self._wake_loop, daemon=True, name="wake").start()
                hint = 'say "Hey Jarvis", press Enter to talk, or type' if self.wake else "press Enter to talk, or type"
                print(f"{DIM}Voice ready (speech recognition on {self.voice.stt.device}): {hint}.{RESET}")
            except Exception as e:
                logger.exception("Voice init failed")
                print(f"{YELLOW}Voice unavailable ({e}); text mode.{RESET}")
                self.voice = None

        if self.cfg.get("tray", True):
            try:
                from jarvis.ui.tray import Tray

                self.tray = Tray({
                    "toggle_wake": lambda: self.q.put(("text", "/wake")),
                    "toggle_speech": lambda: self.q.put(("text", "/unmute" if not self.speech_on else "/mute")),
                    "gaming": lambda: self.q.put(("text", "/gaming")),
                    "free_gpu": lambda: self.q.put(("text", "/unload")),
                    "quit": lambda: self.q.put(("quit",)),
                    "open_hud": lambda: self.hud.open() if self.hud else None,
                    "state": lambda: {"wake": self.wake_on, "speech": self.speech_on, "gaming": self.gaming.active},
                })
                self.tray.start()
            except Exception:
                logger.exception("Tray failed")

        self.gaming.start_watcher()
        self.reminders.start()
        if self.proactive:
            self.proactive.start()
        threading.Thread(target=self._input_loop, daemon=True, name="input").start()
        if self.hud:
            self._flags()
            self._publish("notice", text=f"{self.name} online")
            if hcfg.get("open_on_start", True):
                self.hud.open()
        if self.voice:
            self._say(f"{self.name} online.")
        logger.info("JARVIS ready (pid {})", os.getpid())  # start-jarvis.ps1 waits for this line

    # ------------------------------------------------------------ background threads
    def _input_loop(self) -> None:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                self.q.put(("quit",))
                return
            self.q.put(("text", line.strip().lstrip("﻿")))

    def _wake_loop(self) -> None:
        while not self.quit.is_set():
            if not self.wake_on or self.busy.is_set():
                time.sleep(0.2)
                continue
            try:
                if self.wake.wait(self.busy):
                    self.busy.set()
                    self.q.put(("wake",))
            except Exception:
                logger.exception("Wake-word listener error")
                time.sleep(2)

    # ------------------------------------------------------------ helpers
    def _publish(self, type_: str, **data) -> None:
        if self.hud:
            self.hud.publish(type_, **data)

    def _flags(self) -> None:
        self._publish("flags", wake=self.wake_on, mute=not self.speech_on, gaming=self.gaming.active)

    def _on_tool(self, name: str, args: dict, status: str) -> None:
        show_tool_call(name, args, status)
        self._publish("tool", name=name, args=args, status=status)

    def _state(self, s: str) -> None:
        if self.tray:
            self.tray.set_state(s)
        self._publish("state", state=s)

    def _say(self, text: str) -> None:
        if self.voice and self.speech_on:
            try:
                self.voice.tts.speak(text)
            except Exception:
                logger.exception("TTS failed")

    def _gaming_changed(self, active: bool, reason: str) -> None:
        msg = f"Gaming mode {'on' if active else 'off'} ({reason})."
        print(f"\n{YELLOW}{msg}{RESET}")
        self._publish("notice", text=msg)
        self._flags()
        if self.tray:
            self.tray.notify(msg)

    def _reminder_fired(self, kind: str, text: str) -> None:
        """Called from the scheduler thread: toast immediately, speak via the main loop."""
        if kind == "reminder":
            reminders_mod.toast("JARVIS reminder", text)
        self.q.put((kind, text))

    def briefing_prompt(self) -> str:
        city = self.cfg.get("briefing", {}).get("city")
        parts = [f"the weather in {city}" if city else None, "my open tasks", "my pending reminders",
                 "today's calendar events and unread emails" if "list_events" in self.reg.names() else None,
                 "the top 3 news headlines"]
        return ("Give me my daily briefing in a few short spoken sentences: "
                + ", ".join(p for p in parts if p) + ". Start with a short greeting.")

    def _prompt(self) -> None:
        print("\nYou: ", end="", flush=True)

    def confirm(self, tool: Tool, args: dict) -> bool:
        """Approval always comes from the keyboard, never from voice."""
        color = RED if tool.risk is Risk.DANGER else YELLOW
        print(f"\n{color}⚠  JARVIS wants to run {tool.name} [{tool.risk.value}]")
        for k, v in args.items():
            print(f"     {k}: {v}")
        need = "yes" if tool.risk is Risk.DANGER else "y"
        self._publish("confirm", name=tool.name, args=args, risk=tool.risk.value)
        print(f"   Type '{need}' to approve, anything else to cancel: {RESET}", end="", flush=True)
        if self.tray:
            self.tray.notify(f"Approval needed: {tool.name} (see the JARVIS window)")
        self._say("I need your approval in the console.")
        while True:
            event = self.q.get()
            if event[0] == "quit":
                self.q.put(event)
                return False
            if event[0] == "text":
                return event[1].lower() in ({"yes"} if need == "yes" else {"y", "yes"})

    # ------------------------------------------------------------ turns
    def listen_and_handle(self, start_timeout: float = 6) -> None:
        self._state("listening")
        print(f"\n{CYAN}🎙  listening…{RESET}", flush=True)
        time.sleep(0.15)  # let the wake-word stream release the mic
        try:
            text = self.voice.listen(start_timeout)
        except Exception as e:
            logger.exception("Listening failed")
            print(f"{RED}Microphone problem: {e}{RESET}")
            return
        if not text:
            print(f"{DIM}(didn't catch that){RESET}")
            return
        print(f"You said: {text}")
        if self.handle(text) and self.voice:
            follow = self.cfg.get("voice", {}).get("follow_up_seconds", 4)
            if follow:
                self.listen_and_handle(start_timeout=follow)

    def handle(self, text: str, show: bool = True) -> bool:
        """Run one turn. Returns True if a reply was produced."""
        if show:
            self._publish("user", text=text)
        self._state("thinking")
        start = time.perf_counter()
        print(f"{DIM}thinking…{RESET}", end="\r", flush=True)
        try:
            reply = self.orch.handle(text, self.confirm)
        except Exception as e:
            logger.exception("Turn failed")
            print(f"{RED}Something went wrong: {e}. Details in data/logs/jarvis.log{RESET}")
            self._publish("notice", text=f"Error: {e}")
            self._state("idle")
            return False
        where = " · cloud" if getattr(self.brain, "last_backend", "local") == "cloud" else ""
        print(f"{CYAN}{self.name}:{RESET} {reply}  {DIM}({time.perf_counter() - start:.1f}s{where}){RESET}")
        self._publish("reply", text=reply)
        self._state("speaking")
        self._say(reply)
        self._state("idle")
        return True

    def command(self, cmd: str) -> bool:
        """Handle a /command. Returns False to quit."""
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/reset":
            self.orch.reset()
            print(f"{DIM}Conversation cleared.{RESET}")
        elif cmd == "/facts":
            facts = self.memory.list_facts()
            print("\n".join(f"  {f['id']}: {f['fact']}" for f in facts) or "  (nothing saved yet)")
        elif cmd == "/export":
            out = DATA / f"export-{datetime.now():%Y%m%d-%H%M%S}.json"
            out.write_text(json.dumps(self.memory.export(), indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"{DIM}Exported to {out}{RESET}")
        elif cmd == "/tools":
            for n in self.reg.names():
                t = self.reg.get(n)
                print(f"  {n:22} {t.risk.value:8} {t.group}")
        elif cmd == "/unload":
            self.brain.unload()
            print(f"{DIM}Model unloaded from GPU memory.{RESET}")
        elif cmd == "/gaming":
            on = not self.gaming.active
            self.gaming.manual = on
            self.gaming.set(on)
        elif cmd == "/briefing":
            self.handle(self.briefing_prompt(), show=False)
        elif cmd.startswith("/persona"):
            parts = cmd.split()
            names = list_personas()
            if len(parts) == 2 and parts[1] in names:
                self.orch.persona = load_persona(parts[1])
                print(f"{DIM}Personality: {parts[1]}.{RESET}")
            else:
                print(f"{DIM}Usage: /persona <name>. Available: {', '.join(names)}{RESET}")
        elif cmd == "/private":
            self.brain.private = not self.brain.private
            print(f"{DIM}Private mode {'on: everything stays on this PC' if self.brain.private else 'off'}.{RESET}")
        elif cmd == "/hud":
            if self.hud:
                self.hud.open()
            else:
                print(f"{DIM}HUD is disabled in config.{RESET}")
        elif cmd == "/wake":
            if not self.wake:
                print(f"{DIM}Wake word isn't available (voice off or disabled in config).{RESET}")
            else:
                self.wake_on = not self.wake_on
                print(f"{DIM}Wake word {'on' if self.wake_on else 'off'}.{RESET}")
        elif cmd in ("/mute", "/unmute"):
            self.speech_on = cmd == "/unmute" and self.voice is not None
            print(f"{DIM}Speech {'on' if self.speech_on else 'off'}.{RESET}")
        else:
            print("Unknown command. /help for the list.")
        self._flags()
        return True

    def run(self) -> None:
        self.start()
        self._prompt()
        while True:
            event = self.q.get()
            self.busy.set()
            try:
                if event[0] == "quit":
                    break
                if event[0] == "wake":
                    self.listen_and_handle()
                elif event[0] == "reminder":
                    print(f"\n{YELLOW}⏰ Reminder: {event[1]}{RESET}")
                    self._publish("notice", text=f"⏰ Reminder: {event[1]}")
                    self._say(f"Reminder: {event[1]}")
                elif event[0] == "alert":
                    print(f"\n{YELLOW}⚡ {event[1]}{RESET}")
                    self._publish("notice", text=event[1])
                    reminders_mod.toast("JARVIS", event[1])
                    self._say(event[1])
                elif event[0] == "briefing":
                    print(f"\n{CYAN}☀  Daily briefing{RESET}")
                    self.handle(self.briefing_prompt(), show=False)
                elif event[0] == "text":
                    text = event[1]
                    if not text:
                        if self.voice:
                            self.listen_and_handle()
                    elif text.startswith("/"):
                        if not self.command(text):
                            break
                    else:
                        self.handle(text)
            except KeyboardInterrupt:
                print(f"{YELLOW}Cancelled.{RESET}")
            finally:
                self.busy.clear()
            self._prompt()
        self.shutdown()

    def shutdown(self) -> None:
        self.quit.set()
        self.gaming.stop()
        self.reminders.stop()
        print(f"\n{DIM}Saving conversation summary…{RESET}")
        self.orch.summarize_session()
        if self.tray:
            self.tray.stop()
        print(f"{DIM}Goodbye.{RESET}")
        sys.stdout.flush()
        os._exit(0)  # the input thread is blocked in input(); exit immediately


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--debug", action="store_true", help="print debug logs to the console")
    parser.add_argument("--dry-run", action="store_true", help="don't actually run tools that change things")
    parser.add_argument("--text", action="store_true", help="text only (no microphone or speech)")
    args = parser.parse_args()

    if sys.platform == "win32":
        os.system("")  # enable ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")

    ensure_data_dirs()
    setup_logging(args.debug)
    App(args).run()


if __name__ == "__main__":
    main()
