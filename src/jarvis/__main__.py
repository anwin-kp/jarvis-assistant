"""JARVIS console: typing, push-to-talk, 'Hey Jarvis' wake word and tray icon.
Run:  .\\jarvis.ps1 start   (or: uv run python -m jarvis)"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

from loguru import logger

from jarvis.app import build
from jarvis.core.commands import is_kill_command, is_sleep_command, is_stop_command, run_kill_switch
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
        self.local_stt = WhisperSTT(vcfg.get("stt_model", "small"), vcfg.get("stt_device", "cuda"),
                                    vcfg.get("stt_compute_type", "float16"), download_root=str(DATA / "models"),
                                    vocabulary=vcfg.get("vocabulary"), languages=vcfg.get("languages"))
        self.stt = self.local_stt
        if vcfg.get("stt_engine", "groq") == "groq":
            from jarvis.core.config import get_secret

            if key := get_secret("groq_api_key"):
                from jarvis.stt.groq_stt import GroqSTT

                self.stt = GroqSTT(key, vcfg.get("groq_stt_model", "whisper-large-v3-turbo"),
                                   vcfg.get("vocabulary"), vcfg.get("languages"))
            else:
                logger.info("stt_engine groq needs a Groq key (set-key groq); using local Whisper")
        self.last_engine = self.stt.device
        self.keep_recordings = int(vcfg.get("keep_recordings", 50))
        self.jarvis_print = None  # JARVIS's own voice fingerprint, to ignore echo when you interrupt
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
        try:
            text, lang = self.stt.transcribe(audio, self.cfg.get("language"))
            self.last_engine = self.stt.device
        except Exception as e:  # offline / rate-limited: local Whisper
            logger.warning("{} recognition failed ({}); using local Whisper", self.stt.device, e)
            text, lang = self.local_stt.transcribe(audio, self.cfg.get("language"))
            self.last_engine = self.local_stt.device
        self.last_lang = lang if text else self.last_lang
        self._save(audio, text, lang)
        return text

    def _save(self, audio, text: str, lang: str) -> None:
        """Keep your last N utterances locally (data/voice-log) so recognition can be measured on YOUR voice."""
        if self.keep_recordings <= 0:
            return
        try:
            import json

            from jarvis.stt.groq_stt import to_wav

            folder = DATA / "voice-log"
            folder.mkdir(parents=True, exist_ok=True)
            stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (folder / f"{stem}.wav").write_bytes(to_wav(audio))
            (folder / f"{stem}.json").write_text(json.dumps({"text": text, "lang": lang, "engine": self.last_engine}),
                                                 encoding="utf-8")
            for old in sorted(folder.glob("*.wav"))[:-self.keep_recordings]:
                old.unlink(missing_ok=True)
                old.with_suffix(".json").unlink(missing_ok=True)
        except Exception:
            logger.debug("Could not save recording")

    def build_jarvis_print(self) -> None:
        """Fingerprint JARVIS's own voice so the interrupt detector can ignore it (echo from speakers)."""
        try:
            import numpy as np

            from jarvis.audio.voiceid import VoiceID

            vid = VoiceID(DATA / "models" / "voiceid")
            v = getattr(self.tts, "voice", None)
            if v is None:
                return
            clips = []
            for text in ("Good evening. All systems are online.", "The weather today is clear with light winds.",
                         "I have added that to your reminders."):
                a = np.concatenate([c.audio_float_array for c in v.synthesize(text)])
                rate = v.config.sample_rate
                clips.append(np.interp(np.arange(0, len(a), rate / 16000), np.arange(len(a)), a).astype(np.float32))
            embs = np.array([vid.embed(c) for c in clips])
            self.jarvis_print = embs.mean(0) / np.linalg.norm(embs.mean(0))
            self._vid = vid
            logger.info("JARVIS voice fingerprint ready (echo rejection for interrupts)")
        except Exception as e:
            logger.warning("Echo rejection unavailable: {}", e)

    def sounds_like_jarvis(self, audio) -> bool:
        if self.jarvis_print is None or audio.size < 16000 * 0.6:
            return False
        score = float(self._vid.embed(audio) @ self.jarvis_print)
        logger.debug("Voice fingerprint vs JARVIS: {:.2f}", score)
        return score > self.cfg.get("echo_voice_threshold", 0.55)


def setup_logging(debug: bool) -> None:
    logger.remove()
    logger.add(DATA / "logs" / "jarvis.log", rotation="5 MB", retention=5, level="DEBUG", encoding="utf-8")
    if debug:
        logger.add(sys.stderr, level="DEBUG")


class App:
    def __init__(self, args):
        self.cfg, self.orch, self.brain, self.reg, self.memory, _ = build(dry_run=args.dry_run)
        self.reg.on_call = self._on_tool
        self.reg.confirm_fn = self.confirm
        self.hud = None
        self.learner = None  # learns about the user while idle (jarvis/memory/learner.py)
        self.name = self.cfg.get("assistant_name", "JARVIS")
        self.q: queue.Queue = queue.Queue()
        self.busy = threading.Event()  # set while a turn runs: pauses the wake-word listener
        self.quit = threading.Event()
        self.voice = self.wake = self.tray = None
        self.speech_on = False
        self.listen_mode = "push"  # always | wake_word | push
        self.sleeping = False  # always-mode paused until 'Hey Jarvis'
        self._speech_q: queue.Queue = queue.Queue()
        self._barged = False  # you interrupted: drop the rest of the current reply
        self.engine = None  # AudioEngine: the only owner of the microphone
        self._current_tts = None  # the voice speaking right now (so interrupt stops exactly that one)
        self._spoke_at = 0.0  # when JARVIS last finished speaking
        self._stt_q: queue.Queue = queue.Queue()  # utterances waiting for speech recognition
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
                self.hud.on_stop = self.interrupt
                self.hud.on_input = self.preempt
                self.hud.memory_api = self._memory_api
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
                if vcfg.get("barge_in", True):
                    threading.Thread(target=self.voice.build_jarvis_print, daemon=True, name="voiceprint").start()
                self.speech_on = True
                mode = vcfg.get("listen_mode", "always")
                if vcfg.get("wake_word", True) or mode == "wake_word":
                    try:
                        from jarvis.audio.wakeword import WakeWord

                        self.wake = WakeWord(vcfg.get("wake_threshold", 0.5))
                    except Exception:
                        logger.exception("Wake word unavailable")
                self.listen_mode = mode if mode in ("always", "push") or self.wake else "push"
                self._start_engine(vcfg)
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
        self._start_learner()
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
            self._state("speaking")
            self._say(f"{self.name} online.")
        self._state("ready")  # startup done: the HUD leaves "initializing" (it used to wait for the first speech)
        logger.info("JARVIS ready (pid {})", os.getpid())  # start-jarvis.ps1 waits for this line

    def _memory_api(self, action: str, msg: dict) -> list[dict]:
        """HUD Memory panel: list what JARVIS knows, forget a fact, or teach one in your own words."""
        if action == "forget":
            self.memory.forget(int(msg.get("id", 0)))
        elif action == "teach" and (text := " ".join(str(msg.get("text", "")).split())[:300]):
            learned = []
            if self.learner:  # same checked extraction as conversations: "I love biryani" -> "[food] Loves biryani."
                try:
                    learned = self.learner.apply(self.learner.extract(f"User: {text}"), text)
                except Exception:
                    logger.exception("Teaching failed")
            if not learned:
                from jarvis.memory.learner import clean_fact

                if fact := clean_fact(f"[other] In their own words: {text}"):
                    self.memory.learn(fact)
        return self.memory.list_facts()

    def _start_learner(self) -> None:
        mcfg = self.cfg.get("memory") or {}
        if not mcfg.get("learn", True) or self.args.dry_run:
            return
        from jarvis.memory.learner import Learner, ollama_chat

        self.learner = Learner(
            self.memory, ollama_chat(self.brain),
            is_busy=lambda: self.busy.is_set() or self.speaking or self.gaming.active or self.sleeping,
            summarize=self.orch.summarize_if_idle, idle_s=mcfg.get("learn_idle_seconds", 15),
            summary_idle_s=self.orch.idle_seconds)
        self.learner.on_learned = lambda facts: self._publish(
            "notice", text="Learned: " + "; ".join(re.sub(r"^\[\w+\]\s*", "", f) for f in facts[:3]))
        self.learner.start()

    # ------------------------------------------------------------ background threads
    @property
    def speaking(self) -> bool:
        return self._speech_q.unfinished_tasks > 0

    def preempt(self, text: str = "") -> None:
        """A HUD button or typed message takes priority: if JARVIS is talking or working, stop that now
        (voice, rest of the reply, AI generation, app task) so the new input runs straight away."""
        if getattr(self, "_awaiting_confirm", False):
            return  # it's the answer to an approval request, which is part of the current turn
        if self.speaking or self.busy.is_set():
            logger.info("HUD input cuts in: {}", text[:40] or "(talk)")
            self.interrupt(f"HUD: {text or 'talk'}", force=True)

    def interrupt(self, text: str = "stop", force: bool = False) -> bool:
        """Stop talking right now: HUD Stop button, 'stop' typed or said while JARVIS talks or thinks,
        or talking over JARVIS (force). Stops the voice, the rest of the reply and the AI's generation."""
        if not force and (not (self.speaking or self.busy.is_set()) or not is_stop_command(text)):
            return False
        self._barged = True
        while True:  # drop sentences still waiting to be spoken
            try:
                self._speech_q.get_nowait()
                self._speech_q.task_done()
            except queue.Empty:
                break
        tts = self._current_tts  # the voice that is talking right now
        if tts is not None and hasattr(tts, "stop"):
            tts.stop()
        brain = getattr(self.brain, "local", self.brain)
        if hasattr(brain, "cancel"):
            brain.cancel.set()
        if getattr(self.reg, "operator_cancel", None):
            self.reg.operator_cancel.set()  # stop an app task in progress
        reason = text if text.startswith("HUD: ") else ("you talked over JARVIS" if force else text)
        logger.info("Interrupted ({})", reason)
        return True

    # ------------------------------------------------------------ audio engine (the only mic owner)
    def _engine_mode(self) -> str:
        if self.sleeping:
            return "wake" if self.wake else "push"
        return {"always": "always", "wake_word": "wake", "push": "push"}[self.listen_mode]

    def _start_engine(self, vcfg: dict) -> None:
        from jarvis.audio.engine import AudioEngine

        self.engine = AudioEngine(
            self._on_audio_event, silence_ms=vcfg.get("silence_ms", 750), min_speech_ms=vcfg.get("min_speech_ms", 400),
            max_seconds=vcfg.get("max_seconds", 15), barge_floor=vcfg.get("barge_in_rms", 0.02),
            echo_tail_ms=vcfg.get("echo_tail_ms", 700),
            sounds_like_jarvis=self.voice.sounds_like_jarvis,
            wake_detector=self.wake.process if self.wake else None)
        self.engine.set_mode(self._engine_mode())
        self.engine.start()
        threading.Thread(target=self._stt_worker, daemon=True, name="recognition").start()

    def _sync_engine(self) -> None:
        if self.engine:
            self.engine.set_mode(self._engine_mode())

    def _on_audio_event(self, kind: str, data) -> None:
        """Runs on the audio-engine thread: keep it quick."""
        if kind == "level":
            self._level(data)
        elif kind == "speech_start":
            self._state("listening")
        elif kind == "barge":
            if self.cfg.get("voice", {}).get("barge_in", True):
                self.interrupt(force=True)
                self._publish("user_pending")  # show the interrupt in the chat right away
        elif kind == "wake":
            if self.sleeping:
                self.wake_up()
            self._publish("notice", text="listening")
        elif kind in ("noise", "capture_timeout"):
            if kind == "noise" and data:  # a very short interrupt: that was a "stop"
                self._publish("user", text="(stop)")
            if not self.busy.is_set():
                self._state("ready")
        elif kind == "utterance":
            self._state("thinking")
            if not data.barge:  # (an interrupt already showed its bubble)
                self._publish("user_pending")  # the bubble appears now; text fills in after recognition
            self._stt_q.put(data)

    def _stt_worker(self) -> None:
        """Speech recognition runs here, not in the main loop, so 'stop' works even while JARVIS is busy."""
        while not self.quit.is_set():
            utt = self._stt_q.get()
            if utt.barge and utt.voiced_ms < 600:  # a short burst over JARVIS's voice means "stop"
                self.interrupt(force=True)
                self._publish("user", text="(stop)")
                self._state("ready")
                continue
            t = time.perf_counter()
            try:
                text = self.voice.transcribe(utt.audio)
            except Exception:
                logger.exception("Recognition failed")
                text = ""
            took = time.perf_counter() - t
            if not text:
                self._publish("user_cancel")
                if not self.busy.is_set():
                    self._state("ready")
                continue
            # 1-2 words over JARVIS that aren't a known command ("He's moved" for a mumbled "stop") = stop
            short_interrupt = utt.barge and len(text.split()) <= 2 and not self.fast.matches(text)
            just_spoke = time.monotonic() - self._spoke_at < 5  # "stop" right after JARVIS talked
            if short_interrupt or (is_stop_command(text) and (utt.barge or self.speaking or self.busy.is_set()
                                                              or just_spoke)):
                self.interrupt(text, force=True)  # "stop" is for JARVIS's voice, not your music
                self._publish("user", text=text)
                self._state("ready")
                continue
            self.q.put(("heard", text, utt.after_wake, took))

    def _input_loop(self) -> None:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                self.q.put(("quit",))
                return
            self.q.put(("text", line.strip().lstrip("﻿")))

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
                    self._current_tts = tts
                    if self.engine:
                        self.engine.set_speaking(True)  # mic now only accepts you talking over JARVIS
                    try:
                        tts.speak(text, should_stop=lambda: self._barged)
                    finally:
                        self._current_tts = None
                        self._spoke_at = time.monotonic()
                        if self.engine and self._speech_q.unfinished_tasks <= 1:  # no more sentences waiting
                            self.engine.set_speaking(False)
            except Exception:
                logger.exception("TTS failed")
            finally:
                self._speech_q.task_done()

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
        """Speak and wait until done (the audio engine ignores JARVIS's own voice and its echo)."""
        if self.voice and self.speech_on:
            if text.strip() and not self._barged:
                self._speech_q.put(text)
            self._speech_q.join()

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
        self._awaiting_confirm = True  # the next typed message answers this; it must not cut in on the turn
        self._publish("confirm", name=tool.name, args=args, risk=tool.risk.value)
        print(f"   Type '{need}' to approve, anything else to cancel: {RESET}", end="", flush=True)
        if self.tray:
            self.tray.notify(f"Approval needed: {tool.name} (see the JARVIS window)")
        self._say("I need your approval in the console.")
        try:
            while True:
                event = self.q.get()
                if event[0] == "quit":
                    self.q.put(event)
                    return False
                if event[0] == "text":
                    return event[1].lower() in ({"yes"} if need == "yes" else {"y", "yes"})
        finally:
            self._awaiting_confirm = False

    # ------------------------------------------------------------ turns
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
        self._sync_engine()
        print(f"{DIM}{msg}{RESET}")
        self._publish("reply", text=msg)
        self._say(msg)
        self._flags()
        self._state("idle")

    def wake_up(self) -> None:
        self.sleeping = False
        self._sync_engine()
        self._flags()

    def handle_attachments(self, text: str, ids: list[str]) -> bool:
        from jarvis.ui import attachments

        paths = [p for p in (attachments.resolve(i) for i in ids) if p]
        names = ", ".join(attachments.display_name(p) for p in paths)
        self._publish("user", text=text, files=[{"id": p.name, "name": attachments.display_name(p),
                                                 "image": p.suffix.lower() in attachments.IMAGE_EXT} for p in paths])
        print(f"{DIM}[attached: {names}]{RESET} {text}")
        self._state("thinking")
        prompt = attachments.compose(text, paths, self.cfg)
        idx = getattr(self.reg, "doc_index", None)
        if idx:
            idx.start_background()  # make them searchable later
        # documents stay on this PC (like files); image descriptions already came from the cloud vision model
        private = any(p.suffix.lower() not in attachments.IMAGE_EXT for p in paths)
        return self.handle(prompt, show=False, route_text=text, private=private)

    def handle(self, text: str, show: bool = True, route_text: str | None = None, private: bool = False) -> bool:
        """Run one turn. Returns True if a reply was produced. With route_text (attachments), commands are
        only taken from the user's own words, never from inside a file."""
        words = text if route_text is None else route_text
        if self.learner:
            self.learner.touch()  # learning waits for a quiet moment
        if is_kill_command(words):
            self.kill()
            return False
        if show:
            self._publish("user", text=text)
        self._barged = False
        start = time.perf_counter()
        quick = self.fast.try_handle(text, self.confirm) if route_text is None else None  # instant commands
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
            reply = self.orch.handle(text, self.confirm, on_sentence=stream, route_text=route_text, private=private)
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
                self._sync_engine()
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
            self._barged = False  # each event starts fresh (an old interrupt never silences later speech)
            quiet = False
            try:
                if event[0] == "quit":
                    break
                if event[0] == "heard":
                    _, text, after_wake, took = event
                    self._stt_time = took
                    print(f"\nYou said: {text}")
                    if self.voice_turn(text, after_wake) and self.engine and self._engine_mode() == "wake":
                        follow = self.cfg.get("voice", {}).get("follow_up_seconds", 4)
                        if follow:  # wake-word mode: keep listening briefly for a follow-up
                            self.engine.request_capture(follow, after_wake=True)
                    self._state("ready")  # turn finished: back to listening
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
                    if len(event) > 2 and event[2]:  # message with attachments from the HUD
                        self.handle_attachments(text, event[2])
                    elif not text:
                        if self.engine:  # Enter / HUD Talk: push-to-talk
                            self._state("listening")
                            self.engine.request_capture(6)
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
        if self.engine:
            self.engine.stop()
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


_INSTANCE_LOCK = None


def single_instance(name: str = "Local\\JARVIS-assistant-instance") -> bool:
    """One JARVIS per user session: a named mutex held for the life of the process (released by Windows
    when it exits, even on a crash). False if another JARVIS already holds it."""
    global _INSTANCE_LOCK
    if sys.platform != "win32":
        return True
    import win32api
    import win32event
    import winerror

    _INSTANCE_LOCK = win32event.CreateMutex(None, False, name)
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--debug", action="store_true", help="print debug logs to the console")
    parser.add_argument("--dry-run", action="store_true", help="don't actually run tools that change things")
    parser.add_argument("--text", action="store_true", help="text only (no microphone or speech)")
    parser.add_argument("--background", action="store_true",
                        help="no console: use the HUD/voice; output goes to data/logs/console.log")
    args = parser.parse_args()

    if not single_instance():
        # two launches at once (shortcut + autostart...) gave two JARVIS: both on the mic, one without the HUD
        print("JARVIS is already running.", file=sys.stderr)
        sys.exit(0)
    ensure_data_dirs()
    from jarvis.agent.operator import make_dpi_aware

    make_dpi_aware()
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
