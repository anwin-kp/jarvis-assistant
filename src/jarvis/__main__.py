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
from jarvis.core.commands import is_kill_command, is_sleep_command, run_kill_switch
from jarvis.core.config import DATA, ensure_data_dirs, list_personas, load_persona
from jarvis.core.fast_intents import FastIntents
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
  /listen   cycle listening mode: always listening -> wake word -> push-to-talk
  /sleep    pause listening until \"Hey Jarvis\" (or say \"Jarvis, go to sleep\")
  /mute     stop speaking replies (/unmute to resume)
  /quit     exit (saves a conversation summary)
  /kill     kill switch: stop JARVIS, HUD, model and Ollama (or say \"Jarvis, shut down\")"""

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
                              vcfg.get("stt_compute_type", "float16"), download_root=str(DATA / "models"),
                              vocabulary=vcfg.get("vocabulary"), languages=vcfg.get("languages"))
        self.last_lang: str | None = None
        self.voices = {k.lower(): v for k, v in (vcfg.get("voices") or {}).items()}
        self._tts_cache: dict = {}
        self.voiceid = None
        idcfg = vcfg.get("voice_id") or {}
        if idcfg.get("enabled"):
            from jarvis.audio.voiceid import VoiceID

            self.voiceid = VoiceID(DATA / "models" / "voiceid", idcfg.get("threshold", 0.45))
            if not self.voiceid.enrolled:
                logger.warning("Voice ID is on but no voice is enrolled: run .\\jarvis.ps1 enroll-voice")
                self.voiceid = None
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

    def tts_for(self, lang: str | None):
        """The Piper voice for the language you spoke (config voice.voices), else the default voice."""
        name = self.voices.get(lang or "")
        if not name or name == self.cfg.get("piper_voice"):
            return self.tts
        if name not in self._tts_cache:
            try:
                from jarvis.tts.piper_tts import PiperTTS

                self._tts_cache[name] = PiperTTS(name, DATA / "models" / "piper", self.cfg.get("tts_speed", 1.0))
            except Exception as e:
                logger.warning("Voice {} unavailable: {}", name, e)
                self._tts_cache[name] = self.tts
        return self._tts_cache[name]

    def is_owner(self, audio) -> bool:
        if not self.voiceid:
            return True
        ok, score = self.voiceid.is_owner(audio)
        if not ok:
            logger.info("Ignored speech from another voice (similarity {:.2f})", score)
        return ok

    def transcribe(self, audio) -> str:
        if not audio.size or not self.is_owner(audio):
            return ""
        text, lang = self.stt.transcribe(audio, self.cfg.get("language"))
        self.last_lang = lang if text else self.last_lang
        return text

    def listen(self, start_timeout: float = 6) -> str:
        from jarvis.audio.mic import record_until_silence

        audio = record_until_silence(self.cfg.get("max_seconds", 15), self.cfg.get("silence_ms", 800),
                                     start_timeout=start_timeout)
        return self.transcribe(audio)


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
        self.listen_mode = "push"  # always | wake_word | push
        self.sleeping = False  # always-mode paused until 'Hey Jarvis'
        self._speech_q: queue.Queue = queue.Queue()
        self._barged = False
        self._streamed: list[str] = []
        self.args = args

        # created in app.build() (so evals/tests see the same tools); the app wires up the callbacks
        self.gaming = self.reg.gaming
        self.gaming.on_change = self._gaming_changed
        self.reminders = self.reg.reminders
        self.fast = FastIntents(self.reg, self.cfg)
        self._first_word: float | None = None
        self._stt_time = 0.0
        self.reminders.on_fire = self._reminder_fired

        pcfg = self.cfg.get("proactive", {})
        events_fn = None
        if "list_events" in self.reg.names() and pcfg.get("meeting_reminder_minutes", 10):
            from jarvis.integrations.google import upcoming_events as events_fn
        self.proactive = Proactive(lambda m: self.q.put(("alert", m)), pcfg.get("battery_percent", 15),
                                   pcfg.get("disk_free_gb", 10), break_minutes=pcfg.get("break_reminder_minutes", 0),
                                   events_fn=events_fn, meeting_minutes=pcfg.get("meeting_reminder_minutes", 10),
                                   ) if pcfg.get("enabled", True) else None
        self.telegram = None

    # ------------------------------------------------------------ setup
    def start(self) -> None:
        hcfg = self.cfg.get("hud", {})
        if hcfg.get("enabled", True):
            try:
                from jarvis.ui.hud import Hud

                self.hud = Hud(self.q, hcfg.get("port", 8765))
                self.hud.start()
                self._publish("state", state="booting")
                if hcfg.get("open_on_start", True):
                    time.sleep(0.6)  # let the server bind before the window connects
                    self.hud.open()
            except Exception:
                logger.exception("HUD failed to start")
                self.hud = None
        try:
            self.brain.check()
            threading.Thread(target=self._warmup, daemon=True, name="warmup").start()
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
                threading.Thread(target=self._speaker_loop, daemon=True, name="speaker").start()
                self.speech_on = True
                mode = vcfg.get("listen_mode", "always")
                if vcfg.get("wake_word", True) or mode == "wake_word":
                    try:
                        from jarvis.audio.wakeword import WakeWord

                        self.wake = WakeWord(vcfg.get("wake_threshold", 0.5))
                    except Exception:
                        logger.exception("Wake word unavailable")
                self.listen_mode = mode if mode in ("always", "push") or self.wake else "push"
                threading.Thread(target=self._listen_loop, daemon=True, name="listen").start()
                hint = {"always": "just talk, I'm always listening (say 'Jarvis, go to sleep' to pause)",
                        "wake_word": 'say "Hey Jarvis"', "push": "press Enter to talk"}[self.listen_mode]
                print(f"{DIM}Voice ready (speech recognition on {self.voice.stt.device}): {hint}.{RESET}")
            except Exception as e:
                logger.exception("Voice init failed")
                print(f"{YELLOW}Voice unavailable ({e}); text mode.{RESET}")
                self.voice = None

        if self.cfg.get("tray", True):
            try:
                from jarvis.ui.tray import Tray

                self.tray = Tray({
                    "toggle_wake": lambda: self.q.put(("text", "/listen")),
                    "toggle_speech": lambda: self.q.put(("text", "/unmute" if not self.speech_on else "/mute")),
                    "gaming": lambda: self.q.put(("text", "/gaming")),
                    "free_gpu": lambda: self.q.put(("text", "/unload")),
                    "quit": lambda: self.q.put(("quit",)),
                    "open_hud": lambda: self.hud.open() if self.hud else None,
                    "state": lambda: {"wake": self.listen_mode != "push" and not self.sleeping,
                                      "speech": self.speech_on, "gaming": self.gaming.active},
                })
                self.tray.start()
            except Exception:
                logger.exception("Tray failed")

        self.gaming.start_watcher()
        self.reminders.start()
        if getattr(self.reg, "doc_index", None) and self.cfg.get("documents", {}).get("index_on_start", True):
            self.reg.doc_index.start_background()
        if self.proactive:
            self.proactive.start()
        self._start_telegram()
        if not self.args.background:
            threading.Thread(target=self._input_loop, daemon=True, name="input").start()
        if self.hud:
            threading.Thread(target=self._stats_loop, daemon=True, name="stats").start()
        if self.hud:
            self._flags()
            self._publish("notice", text=f"{self.name} online")
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

    def _listen_loop(self) -> None:
        """Hands-free listening. always: record whenever someone speaks; wake_word: wait for 'Hey Jarvis'.
        Paused (via self.busy) while JARVIS thinks or speaks, so it never hears itself."""
        from jarvis.audio.mic import record_until_silence

        v = self.cfg.get("voice", {})
        while not self.quit.is_set():
            mode = "wake_word" if self.sleeping else self.listen_mode
            if self.busy.is_set() or mode == "push" or (mode == "wake_word" and not self.wake):
                time.sleep(0.2)
                continue
            try:
                if mode == "wake_word":
                    if self.wake.wait(self.busy):
                        self.busy.set()
                        self.q.put(("wake",))
                    continue
                time.sleep(0.4)  # let the room go quiet after JARVIS spoke
                if self.busy.is_set():
                    continue
                self._state("ready")
                audio = record_until_silence(v.get("max_seconds", 15), v.get("silence_ms", 800), start_timeout=None,
                                             stop=self.busy, on_level=self._level,
                                             min_speech_ms=v.get("min_speech_ms", 400))
                if audio.size and not self.busy.is_set():
                    self.busy.set()
                    self.q.put(("audio", audio))
            except Exception:
                logger.exception("Listener error")
                time.sleep(2)

    def _level(self, value: float) -> None:
        if self.hud:
            self.hud.publish("level", v=round(value, 3))

    def _stats_loop(self) -> None:
        import subprocess

        import psutil

        while not self.quit.wait(3):
            try:
                gpu = None
                r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                if r.returncode == 0:
                    util, used, total = (float(x) for x in r.stdout.strip().split(",")[:3])
                    gpu = {"util": util, "used": used, "total": total}
                bat = psutil.sensors_battery()
                self._publish("stats", cpu=psutil.cpu_percent(), ram=psutil.virtual_memory().percent, gpu=gpu,
                              battery=bat.percent if bat else None, plugged=bat.power_plugged if bat else None,
                              model=self.brain.model)
            except Exception:
                logger.debug("stats failed")

    # ------------------------------------------------------------ helpers
    def _publish(self, type_: str, **data) -> None:
        if self.hud:
            self.hud.publish(type_, **data)

    def _flags(self) -> None:
        self._publish("flags", mode=self.listen_mode, sleeping=self.sleeping, mute=not self.speech_on,
                      gaming=self.gaming.active, voice=self.voice is not None)

    def _on_tool(self, name: str, args: dict, status: str) -> None:
        show_tool_call(name, args, status)
        self._publish("tool", name=name, args=args, status=status)

    def _state(self, s: str) -> None:
        if self.tray:
            self.tray.set_state(s)
        self._publish("state", state=s)

    def _speaker_loop(self) -> None:
        while True:
            text = self._speech_q.get()
            try:
                if self.speech_on and not self._barged:
                    tts = self.voice.tts_for(self.voice.last_lang)
                    stop = threading.Event()
                    if self.cfg.get("voice", {}).get("barge_in") and hasattr(tts, "stop"):
                        threading.Thread(target=self._barge_monitor, args=(stop, tts), daemon=True).start()
                    try:
                        tts.speak(text)
                    finally:
                        stop.set()
            except Exception:
                logger.exception("TTS failed")
            finally:
                self._speech_q.task_done()

    def _barge_monitor(self, stop: threading.Event, tts) -> None:
        """Interrupt JARVIS by talking over it (best with a headset: speakers can echo into the mic)."""
        import numpy as np
        import sounddevice as sd
        import webrtcvad

        vad, run = webrtcvad.Vad(3), 0
        threshold = self.cfg.get("voice", {}).get("barge_in_rms", 0.03)
        try:
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=480) as s:
                while not stop.is_set():
                    block, _ = s.read(480)
                    rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
                    run = run + 1 if rms >= threshold and vad.is_speech(block.tobytes(), 16000) else 0
                    if run >= 8:  # ~0.25 s of clear speech
                        self._barged = True
                        tts.stop()
                        logger.info("Barge-in: stopped speaking")
                        return
        except Exception:
            logger.exception("Barge-in monitor failed")

    def _warmup(self) -> None:
        """Load the AI into the GPU and pre-fill its prompt cache, so the first question is fast too."""
        from jarvis.llm.tool_groups import pick_groups

        t = time.perf_counter()
        try:
            msgs = [{"role": "system", "content": self.orch.system_prompt()}, {"role": "user", "content": "hi"}]
            self.brain.warmup(msgs, self.reg.schemas(pick_groups("hi")))
            if getattr(self.orch, "semantic", None):
                self.orch.semantic.pick("hello")  # loads the embedding model + tool vectors
            logger.info("Warm-up done in {:.1f}s", time.perf_counter() - t)
        except Exception as e:
            logger.warning("Warm-up failed: {}", e)

    def _timing(self, start: float, path: str) -> None:
        total = time.perf_counter() - start
        first = (self._first_word - start) if self._first_word else total
        t = {"stt": round(self._stt_time, 2), "first": round(first, 2), "total": round(total, 2), "path": path}
        logger.info("Timing: heard {stt}s · first word {first}s · total {total}s · {path}", **t)
        self._publish("timing", **t)
        self._stt_time, self._first_word = 0.0, None

    def _speak_async(self, sentence: str) -> None:
        """Streaming: speak each sentence as soon as the model finishes writing it."""
        if self._first_word is None:
            self._first_word = time.perf_counter()
        if self._barged:
            return
        self._streamed.append(sentence)
        self._state("speaking")
        self._speech_q.put(sentence)

    def _say(self, text: str) -> None:
        if self.voice and self.speech_on:
            was_busy = self.busy.is_set()
            self.busy.set()  # pause the microphone while speaking, so JARVIS never hears itself
            try:
                if text.strip():
                    self._speech_q.put(text)
                self._speech_q.join()
            finally:
                if not was_busy:
                    if not self._barged:
                        time.sleep(0.4)  # let the echo die down
                    self.busy.clear()

    def _finish_speech(self, reply: str) -> None:
        """Speak whatever part of the reply wasn't already streamed, then wait until speech ends."""
        rest = reply
        for s in self._streamed:
            rest = rest.replace(s, "", 1)
        self._streamed = []
        rest = rest.strip()
        if self._barged:  # you interrupted: stop talking and listen
            self._barged = False
            return
        self._say(rest if len(rest.strip(" .,!?")) > 1 else "")

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

    def _start_telegram(self) -> None:
        tcfg = self.cfg.get("telegram") or {}
        from jarvis.core.config import get_secret

        token = get_secret("telegram_api_key")
        if not tcfg.get("enabled", True) or not token:
            return
        from jarvis.integrations.telegram import TelegramBridge

        def unknown(chat_id, name):
            self.q.put(("notice", f"Telegram: {name or 'someone'} (chat id {chat_id}) messaged your bot. If that's "
                                  f"you, set telegram.allowed_chat_id: {chat_id} in config.yaml and restart."))

        self.telegram = TelegramBridge(token, tcfg.get("allowed_chat_id"),
                                       lambda chat_id, text: self.q.put(("remote", chat_id, text)), unknown)
        self.telegram.start()
        logger.info("Telegram remote active")

    def handle_remote(self, chat_id: int, text: str) -> None:
        """A message from your phone: answer by text; actions that need approval are refused."""
        from jarvis.integrations.telegram import remote_confirm

        print(f"\n📱 Telegram: {text}")
        self._publish("notice", text=f"📱 {text}")
        if is_kill_command(text):
            self.telegram.send(chat_id, "Shutting down JARVIS.")
            self.kill()
        try:
            reply = self.orch.handle(text, remote_confirm)
        except Exception as e:
            logger.exception("Remote turn failed")
            reply = f"Something went wrong: {e}"
        print(f"{CYAN}{self.name} → phone:{RESET} {reply}")
        self._publish("reply", text=f"📱 {reply}")
        self.telegram.send(chat_id, reply)

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
    def listen_and_handle(self, start_timeout: float = 6, after_wake: bool = False) -> None:
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
            self._state("idle")
            return
        print(f"You said: {text}")
        if self.voice_turn(text, after_wake) and self.listen_mode == "wake_word" and not self.sleeping:
            follow = self.cfg.get("voice", {}).get("follow_up_seconds", 4)
            if follow:
                self.listen_and_handle(start_timeout=follow)

    def handle_audio(self, audio) -> bool:
        """Speech captured by always-listening. Returns False if it was just noise (nothing printed)."""
        self._state("thinking")
        t = time.perf_counter()
        text = self.voice.transcribe(audio)
        self._stt_time = time.perf_counter() - t
        if not text:
            self._state("idle")
            return False
        print(f"\nYou said: {text}")
        self.voice_turn(text, after_wake=False)
        return True

    def voice_turn(self, text: str, after_wake: bool) -> bool:
        if is_kill_command(text, after_wake=after_wake):
            self.kill()
        if is_sleep_command(text):
            self.go_to_sleep()
            return False
        return self.handle(text)

    def go_to_sleep(self) -> None:
        self._publish("user", text="(go to sleep)")
        if self.wake:
            self.sleeping = True
            msg = 'Going to sleep. Say "Hey Jarvis" to wake me.'
        else:
            self.listen_mode = "push"
            msg = "I've stopped listening. Press Enter or Talk to speak to me."
        print(f"{DIM}{msg}{RESET}")
        self._publish("reply", text=msg)
        self._say(msg)
        self._flags()
        self._state("idle")

    def wake_up(self) -> None:
        self.sleeping = False
        self._flags()

    def handle(self, text: str, show: bool = True) -> bool:
        """Run one turn. Returns True if a reply was produced."""
        if is_kill_command(text):
            self.kill()
            return False
        if show:
            self._publish("user", text=text)
        self._barged = False
        start = time.perf_counter()
        quick = self.fast.try_handle(text, self.confirm)  # instant commands: no AI round-trip
        if quick is not None:
            print(f"{CYAN}{self.name}:{RESET} {quick}  {DIM}(instant){RESET}")
            self.orch.add_exchange(text, quick)
            self._publish("reply", text=quick)
            self._first_word = time.perf_counter()
            self._timing(start, "instant")
            self._state("speaking")
            self._say(quick)
            self._state("idle")
            return True
        self._state("thinking")
        print(f"{DIM}thinking…{RESET}", end="\r", flush=True)
        try:
            stream = self._speak_async if (self.voice and self.speech_on) else None
            reply = self.orch.handle(text, self.confirm, on_sentence=stream)
        except Exception as e:
            logger.exception("Turn failed")
            print(f"{RED}Something went wrong: {e}. Details in data/logs/jarvis.log{RESET}")
            self._publish("notice", text=f"Error: {e}")
            self._state("idle")
            return False
        where = " · cloud" if getattr(self.brain, "last_backend", "local") == "cloud" else ""
        path = ("cloud" if where else "local") + (" · thinking" if self.orch.last_think else "")
        self._timing(start, path)
        print(f"{CYAN}{self.name}:{RESET} {reply}  {DIM}({time.perf_counter() - start:.1f}s{where}){RESET}")
        self._publish("reply", text=reply)
        self._state("speaking")
        self._finish_speech(reply)
        self._state("idle")
        return True

    def kill(self) -> None:
        """'Jarvis shut down' / 'Shutdown Jarvis' / /kill: run the kill switch (stops JARVIS, HUD, model, Ollama)."""
        print(f"\n{RED}Shutting down: running the kill switch…{RESET}", flush=True)
        self._publish("notice", text="Shutting down (kill switch)")
        self._say("Shutting down.")
        logger.info("Kill command received; running kill-jarvis.cmd")
        run_kill_switch(visible=not self.args.background)
        time.sleep(15)  # the kill script force-stops this process; exit anyway if it somehow didn't
        os._exit(0)

    def command(self, cmd: str) -> bool:
        """Handle a /command. Returns False to quit."""
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/kill":
            self.kill()
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
        elif cmd in ("/listen", "/wake"):
            if not self.voice:
                print(f"{DIM}Voice is off.{RESET}")
            else:
                order = ["always", "wake_word", "push"] if self.wake else ["always", "push"]
                self.sleeping = False
                self.listen_mode = order[(order.index(self.listen_mode) + 1) % len(order)]
                desc = {"always": "always listening", "wake_word": 'wake word ("Hey Jarvis")', "push": "push-to-talk"}
                print(f"{DIM}Listening mode: {desc[self.listen_mode]}.{RESET}")
        elif cmd == "/sleep":
            self.go_to_sleep()
        elif cmd == "/wakeup":
            self.wake_up()
            print(f"{DIM}Awake and listening.{RESET}")
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
            quiet = False
            try:
                if event[0] == "quit":
                    break
                if event[0] == "audio":
                    quiet = not self.handle_audio(event[1])
                elif event[0] == "wake":
                    if self.sleeping:
                        self.wake_up()
                    self.listen_and_handle(after_wake=True)
                elif event[0] == "reminder":
                    print(f"\n{YELLOW}⏰ Reminder: {event[1]}{RESET}")
                    self._publish("notice", text=f"⏰ Reminder: {event[1]}")
                    self._say(f"Reminder: {event[1]}")
                elif event[0] == "alert":
                    print(f"\n{YELLOW}⚡ {event[1]}{RESET}")
                    self._publish("notice", text=event[1])
                    reminders_mod.toast("JARVIS", event[1])
                    self._say(event[1])
                elif event[0] == "remote":
                    self.handle_remote(event[1], event[2])
                elif event[0] == "notice":
                    print(f"\n{YELLOW}{event[1]}{RESET}")
                    self._publish("notice", text=event[1])
                elif event[0] == "briefing":
                    print(f"\n{CYAN}☀  Daily briefing{RESET}")
                    self.handle(self.briefing_prompt(), show=False)
                elif event[0] == "text":
                    text = event[1]
                    if self.voice and text and not text.startswith("/"):
                        self.voice.last_lang = None  # typed: answer in the default voice
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
            if not quiet:
                self._prompt()
        self.shutdown()

    def shutdown(self) -> None:
        self.quit.set()
        self.gaming.stop()
        self.reminders.stop()
        if self.telegram:
            self.telegram.stop()
        if getattr(self.reg, "mcp", None):
            self.reg.mcp.close()
        if getattr(self.reg, "browser", None):
            try:
                self.reg.browser.close()
            except Exception:
                pass
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
    parser.add_argument("--background", action="store_true",
                        help="no console: use the HUD/voice; output goes to data/logs/console.log")
    args = parser.parse_args()

    ensure_data_dirs()
    if args.background or sys.stdout is None:  # pythonw has no console
        args.background = True
        global CYAN, YELLOW, RED, DIM, RESET
        CYAN = YELLOW = RED = DIM = RESET = ""
        out = open(DATA / "logs" / "console.log", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = out
    elif sys.platform == "win32":
        os.system("")  # enable ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")

    setup_logging(args.debug)
    App(args).run()


if __name__ == "__main__":
    main()
