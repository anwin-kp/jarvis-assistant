"""Text console for JARVIS (Phase 1). Run:  uv run python -m jarvis"""

import argparse
import json
import sys
import time
from datetime import datetime

from loguru import logger

from jarvis.app import build
from jarvis.core.config import DATA, ensure_data_dirs
from jarvis.llm.ollama_brain import BrainUnavailable
from jarvis.tools.registry import Risk, Tool

CYAN, YELLOW, RED, DIM, RESET = "\033[96m", "\033[93m", "\033[91m", "\033[2m", "\033[0m"

HELP = """Commands:
  /help     show this help
  /reset    forget the current conversation (saved facts stay)
  /facts    list what JARVIS remembers about you
  /export   save everything JARVIS knows (facts, summaries, history) to a JSON file
  /tools    list available tools
  /unload   free the GPU memory now (e.g. before gaming)
  /mute     stop speaking replies (/unmute to resume)
  /quit     exit"""


def console_confirm(tool: Tool, args: dict) -> bool:
    color = RED if tool.risk is Risk.DANGER else YELLOW
    print(f"\n{color}⚠  JARVIS wants to run {tool.name} [{tool.risk.value}]")
    for k, v in args.items():
        print(f"     {k}: {v}")
    if tool.risk is Risk.DANGER:
        answer = input(f"   Type 'yes' to approve: {RESET}").strip().lower()
        return answer == "yes"
    answer = input(f"   Approve? [y/N] {RESET}").strip().lower()
    return answer in ("y", "yes")


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
        self.tts = SapiTTS(vcfg.get("tts_rate", 185), vcfg.get("tts_voice"))

    def listen(self) -> str:
        from jarvis.audio.mic import record_until_silence

        audio = record_until_silence(self.cfg.get("max_seconds", 15), self.cfg.get("silence_ms", 800))
        text, _ = self.stt.transcribe(audio, self.cfg.get("language"))
        return text


def setup_logging(debug: bool) -> None:
    logger.remove()
    logger.add(DATA / "logs" / "jarvis.log", rotation="5 MB", retention=5, level="DEBUG", encoding="utf-8")
    if debug:
        logger.add(sys.stderr, level="DEBUG")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--debug", action="store_true", help="print debug logs to the console")
    parser.add_argument("--dry-run", action="store_true", help="don't actually run non-safe tools")
    parser.add_argument("--text", action="store_true", help="text only (no microphone or speech)")
    args = parser.parse_args()

    if sys.platform == "win32":
        import os

        os.system("")  # enable ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")

    ensure_data_dirs()
    setup_logging(args.debug)
    cfg, orch, brain, reg, memory, roots = build(dry_run=args.dry_run)
    reg.on_call = show_tool_call

    try:
        brain.check()
    except BrainUnavailable as e:
        print(f"{RED}{e}{RESET}")
        sys.exit(1)

    name = cfg.get("assistant_name", "JARVIS")
    info = f"Model: {brain.model} · {len(reg.names())} tools · /help for commands"
    print(f"{CYAN}{name} online.{RESET} {DIM}{info}{RESET}")
    if args.dry_run:
        print(f"{YELLOW}Dry-run mode: actions that change things will only be simulated.{RESET}")

    vcfg = cfg.get("voice", {})
    voice = None
    if vcfg.get("enabled", True) and not args.text:
        print(f"{DIM}Loading speech recognition…{RESET}", end="\r", flush=True)
        try:
            voice = Voice(vcfg)
            print(f"{DIM}Voice ready (speech recognition on {voice.stt.device}). "
                  f"Press Enter on an empty line to talk, or type.{RESET}")
            voice.tts.speak(f"{name} online.")
        except Exception as e:
            logger.exception("Voice init failed")
            print(f"{YELLOW}Voice unavailable ({e}); continuing in text mode.{RESET}")
            voice = None
    speak = voice is not None

    while True:
        try:
            prompt = "\nYou (Enter = talk): " if voice else "\nYou: "
            text = input(prompt).strip().lstrip("﻿")  # PowerShell pipes add a BOM
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text and voice:
            print(f"{CYAN}🎙  listening…{RESET}", flush=True)
            try:
                text = voice.listen()
            except Exception as e:
                logger.exception("Listening failed")
                print(f"{RED}Microphone problem: {e}{RESET}")
                continue
            if not text:
                print(f"{DIM}(didn't catch that){RESET}")
                continue
            print(f"You said: {text}")
        if not text:
            continue
        if text.lower() in ("/mute", "/unmute") and voice:
            speak = text.lower() == "/unmute"
            print(f"{DIM}Speech {'on' if speak else 'off'}.{RESET}")
            continue
        if text.startswith("/"):
            cmd = text.lower()
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/reset":
                orch.reset()
                print(f"{DIM}Conversation cleared.{RESET}")
            elif cmd == "/facts":
                facts = memory.list_facts()
                print("\n".join(f"  {f['id']}: {f['fact']}" for f in facts) or "  (nothing saved yet)")
            elif cmd == "/export":
                out = DATA / f"export-{datetime.now():%Y%m%d-%H%M%S}.json"
                out.write_text(json.dumps(memory.export(), indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"{DIM}Exported to {out}{RESET}")
            elif cmd == "/tools":
                for n in reg.names():
                    t = reg.get(n)
                    print(f"  {n:22} {t.risk.value:8} {t.group}")
            elif cmd == "/unload":
                brain.unload()
                print(f"{DIM}Model unloaded from GPU memory.{RESET}")
            else:
                print("Unknown command. /help for the list.")
            continue

        start = time.perf_counter()
        print(f"{DIM}thinking…{RESET}", end="\r", flush=True)
        try:
            reply = orch.handle(text, console_confirm)
        except KeyboardInterrupt:
            print(f"{YELLOW}Cancelled.{RESET}        ")
            continue
        except Exception as e:
            logger.exception("Turn failed")
            print(f"{RED}Something went wrong: {e}. Details in data/logs/jarvis.log{RESET}")
            continue
        elapsed = time.perf_counter() - start
        print(f"{CYAN}{name}:{RESET} {reply}  {DIM}({elapsed:.1f}s){RESET}")
        if speak:
            try:
                voice.tts.speak(reply)
            except Exception:
                logger.exception("TTS failed")

    orch.summarize_session()
    print(f"{DIM}Goodbye.{RESET}")


if __name__ == "__main__":
    main()
