"""First-run / health check: `.\\jarvis.ps1 doctor`. Every check says what's wrong and the exact next step.
Read-only: nothing is installed or changed. Key values are never printed (only whether each key is set)."""

import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import requests

OK, WARN, FAIL = "ok", "warn", "fail"
SETUP = "SETUP.md"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) != (3, 12):
        return Check(FAIL, "Python", f"{v.major}.{v.minor}", "Install Python 3.12 (winget install Python.Python.3.12)")
    if "\\uv\\python" in sys.base_prefix.lower():  # uv's own (unsigned) Python build
        return Check(WARN, "Python", f"3.12 from {sys.base_prefix}",
                     "Smart App Control may block uv's own Python: use python.org's (see SETUP.md)")
    return Check(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")


PACKAGES = {"ollama": "AI model client", "faster_whisper": "local speech recognition", "piper": "voice",
            "sounddevice": "microphone", "webrtcvad": "speech detection", "keyring": "secure keys",
            "fastapi": "HUD"}


def check_packages() -> Check:
    bad = []
    for mod, what in PACKAGES.items():
        try:
            importlib.import_module(mod)
        except Exception as e:  # ImportError, or "An Application Control policy has blocked this file"
            bad.append(f"{mod} ({what}): {str(e)[:60]}")
    if bad:
        blocked = any("Application Control" in b for b in bad)
        fix = "Smart App Control blocked native code: see SETUP.md troubleshooting" if blocked else "Run: uv sync"
        return Check(FAIL, "Packages", "; ".join(bad), fix)
    return Check(OK, "Packages", f"{len(PACKAGES)} core packages import")


def check_ollama(cfg: dict, get=requests.get) -> list[Check]:
    host = cfg.get("llm", {}).get("ollama_host", "http://127.0.0.1:11434")
    want = cfg.get("llm", {}).get("ollama_model", "qwen3:8b")
    try:
        names = {m["name"] for m in get(f"{host}/api/tags", timeout=3).json().get("models", [])}
    except Exception:
        if shutil.which("ollama"):  # installed: start-jarvis starts it, so not a blocker
            return [Check(WARN, "Ollama", "installed, not running", r".\jarvis.ps1 start starts it automatically")]
        return [Check(FAIL, "Ollama", "not installed", "winget install Ollama.Ollama")]
    out = [Check(OK, "Ollama", f"running at {host}")]
    model_ok = want in names or f"{want}:latest" in names
    out.append(Check(OK if model_ok else FAIL, "AI model", want if model_ok else f"{want} not downloaded",
                     "" if model_ok else f"ollama pull {want}   (start-jarvis also downloads it)"))
    emb = (cfg.get("documents") or {}).get("model", "nomic-embed-text")
    if not ({emb, f"{emb}:latest"} & names):
        out.append(Check(WARN, "Embedding model", f"{emb} not downloaded yet", "Downloaded automatically on first use"))
    return out


def check_gpu(run=subprocess.run) -> Check:
    try:
        r = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
        name, total = (x.strip() for x in r.stdout.strip().splitlines()[0].split(","))
    except Exception:
        return Check(WARN, "GPU", "no NVIDIA GPU found", "JARVIS works on the CPU, but replies are slower")
    gb = float(total) / 1024
    return Check(OK if gb >= 6 else WARN, "GPU", f"{name}, {gb:.0f} GB",
                 "" if gb >= 6 else "Under 6 GB VRAM: consider llm.ollama_model: qwen3:4b")


def check_microphone() -> Check:
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        return Check(OK, "Microphone", dev["name"])
    except Exception:
        return Check(FAIL, "Microphone", "no input device", "Plug in a mic, and allow desktop apps in "
                     "Settings > Privacy & security > Microphone")


KEYS = {"groq_api_key": ("Groq", "recommended: smarter answers, better speech recognition, screen vision",
                         "set-key groq"),
        "gemini_api_key": ("Gemini", "backup cloud brain", "set-key gemini"),
        "telegram_api_key": ("Telegram", "phone remote", "set-key telegram"),
        "home_assistant_api_key": ("Home Assistant", "smart home", "set-key home_assistant"),
        "spotify_client_id": ("Spotify", "play your playlists (Premium)", "set-key spotify_client_id")}


def check_keys(get_secret: Callable[[str], str | None]) -> list[Check]:
    out = []
    for key, (name, what, cmd) in KEYS.items():
        try:
            present = bool(get_secret(key))
        except Exception:
            present = False
        out.append(Check(OK if present else (WARN if key == "groq_api_key" else "info"), name,
                         "set" if present else f"not set ({what})", "" if present else f".\\jarvis.ps1 {cmd}"))
    return out


def check_google(data) -> Check:
    folder = data / "google"
    if (folder / "token.json").exists():
        return Check(OK, "Gmail & Calendar", "signed in")
    if (folder / "credentials.json").exists():
        return Check(WARN, "Gmail & Calendar", "credentials found, not signed in", ".\\jarvis.ps1 google-login")
    return Check("info", "Gmail & Calendar", "not set up (optional)", f"see {SETUP} > Gmail & Google Calendar")


def run_all() -> list[Check]:
    from jarvis.core.config import DATA, get_secret, load_config

    cfg = load_config()
    checks = [check_python(), check_packages(), *check_ollama(cfg), check_gpu(), check_microphone(),
              *check_keys(get_secret), check_google(DATA)]
    if (shutil.disk_usage(DATA).free / 2**30) < 5:
        checks.append(Check(WARN, "Disk", "under 5 GB free", "Models and recordings need space"))
    return checks


MARK = {OK: "[ok]  ", WARN: "[!]   ", FAIL: "[X]   ", "info": "[ ]   "}


def report(checks: list[Check]) -> str:
    lines = [f"  {MARK[c.status]}{c.name}: {c.detail}" + (f"\n          -> {c.fix}" if c.fix else "") for c in checks]
    fails = sum(c.status == FAIL for c in checks)
    lines.append("\n  " + (f"{fails} problem(s) to fix before JARVIS can run." if fails else
                           "Ready. Start with: .\\jarvis.ps1 start   ([ ] = optional extras)"))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    result = run_all()
    print("JARVIS doctor\n" + report(result))
    sys.exit(1 if any(c.status == FAIL for c in result) else 0)
