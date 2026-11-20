# JARVIS Assistant

[![CI](https://github.com/anwin-kp/jarvis-assistant/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/anwin-kp/jarvis-assistant/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6)

A **free, local-first** JARVIS-style personal AI assistant for Windows. The AI runs on your own GPU
through [Ollama](https://ollama.com). No paid APIs, works offline, and your voice never leaves your PC.

See [JARVIS_PLAN.md](JARVIS_PLAN.md) for the architecture and roadmap.

## Features

| Area | What JARVIS can do |
|---|---|
| Voice | "Hey Jarvis" wake word, push-to-talk, Whisper speech recognition (GPU), natural offline Piper voice |
| Desktop | Futuristic HUD window (arc reactor, live transcript, tool activity, on-screen approvals), tray icon |
| System | CPU/RAM/disk/battery, processes, volume, brightness, lock, shutdown/restart (cancellable), optional read-only PowerShell |
| Apps & files | Open/close allowlisted apps; search, open, move and delete files (Recycle Bin), limited to allowed folders |
| Web | DuckDuckGo search, read web pages, weather (Open-Meteo), Wikipedia, news (RSS) |
| Memory | Remembers facts about you, summarizes past conversations, `/export` everything |
| Reminders | Reminders and timers (one-time, daily, weekdays, weekly), to-do list, toasts, daily briefing |
| Email & calendar | Gmail (read, draft, send with approval) and Google Calendar, *optional* |
| Gaming mode | Detects your games and frees the GPU automatically |
| Proactive | Warns about low battery and low disk space |
| Personality | `default`, `butler`, `casual`, `terse` (or write your own) |
| Smart home | Home Assistant lights, switches, fans and more, *optional* (locks and alarms are never allowed) |
| Hands-free | Always listening (no button), "Jarvis, go to sleep", optional wake word, interrupt by talking (headset), voice ID (only obeys you) |
| Languages | Speak English, Hindi, Malayalam, Telugu, Marathi and more; JARVIS answers in a matching voice |
| Media & windows | Play/pause/next, play on YouTube, Spotify search, switch/snap/minimize windows, show desktop |
| Clipboard & typing | Summarize/translate what you copied, copy results, type text into any app |
| PC toggles | Dark mode, Wi-Fi/Bluetooth, screenshots, empty recycle bin, open Settings pages |
| Notes | "Take a note…", journal and search, saved as Markdown in Documents\JARVIS Notes |
| Screen | "What's this error on my screen?" via Windows' built-in OCR (offline) |
| Your documents | Ask questions about your PDFs/Word/text files (local semantic search) |
| Browser agent | JARVIS drives its own Edge window to open sites, read, click and fill forms (always asks first, never types passwords) |
| Extensible | Drop-in plugins (`plugins/`), any MCP server (`mcp_servers`), Telegram phone remote |
| Proactive | Low battery/disk, break reminders, upcoming meetings |
| Cloud fallback | Free Groq / Gemini / OpenRouter tiers, *optional*; files, email and memory always stay local |
| Safety | Permission tiers (safe / confirm / danger), keyboard or HUD approvals, audit log, dry-run, kill switch |

## Install

Requirements: Windows 10/11, an NVIDIA GPU with ~6 GB+ VRAM recommended (it also runs on CPU, more slowly).

```powershell
winget install Python.Python.3.12 astral-sh.uv Ollama.Ollama
git clone https://github.com/anwin-kp/jarvis-assistant.git
cd jarvis-assistant
uv sync
ollama pull qwen3:8b        # optional: start-jarvis downloads it if missing
.\start-jarvis.ps1
```

The first start downloads the speech models (~550 MB) and voice (~60 MB).

## Start and stop

Run `.\jarvis.ps1 shortcut` once: it adds a **JARVIS** icon to your Desktop and Start menu (and **Kill JARVIS**
to the Start menu). Clicking it starts everything silently in the background, and the only thing you see is the
HUD. Clicking it again while JARVIS runs just reopens the HUD.


| | Double-click | Or run |
|---|---|---|
| **Start** | **JARVIS** shortcut, or `start-jarvis.cmd` | `.\start-jarvis.ps1 [-Console] [-DryRun] [-Text]` (`-Console` shows the terminal) |
| **Kill** (emergency stop) | `kill-jarvis.cmd` | `.\kill-jarvis.ps1` or say **"Jarvis, shut down"** / **"Shutdown Jarvis"**, or type `/kill` |

**start-jarvis** starts Ollama if needed, downloads the AI model if it's missing, opens JARVIS in its own
window and waits until JARVIS reports it's ready (it tells you if startup fails, and the JARVIS window stays
open to show the error). **kill-jarvis** immediately force-stops JARVIS, its window, the HUD, the AI model in
GPU memory and Ollama, then verifies nothing is left. It's self-contained and logged to `data/logs/kill-jarvis.log`.

## Use

Say **"Hey Jarvis"**, press **Enter** on an empty line to talk, or type. In the HUD you can also click
**Talk** or type. Actions that change things show an approval prompt. Dangerous ones (delete, send email,
shutdown, kill process) need you to type `yes` or click **Approve**, never just a spoken yes.

Console commands: `/help`, `/briefing`, `/gaming`, `/persona <name>`, `/private`, `/mute`, `/wake`, `/hud`,
`/facts`, `/export`, `/reset`, `/unload`, `/quit`.

## Control: `jarvis.ps1`

```powershell
.\jarvis.ps1 start [-DryRun] [-Text] [-Minimized]   # same as start-jarvis.ps1
.\jarvis.ps1 status            # JARVIS / Ollama / model / GPU memory
.\jarvis.ps1 free              # unload the AI model from the GPU
.\jarvis.ps1 stop | restart | stop-all
.\jarvis.ps1 logs [-Follow]
.\jarvis.ps1 test
.\jarvis.ps1 autostart-on | autostart-off             # start with Windows
.\jarvis.ps1 google-login                             # connect Gmail/Calendar
.\jarvis.ps1 set-key groq|gemini|openrouter|home_assistant
```

## Optional setup

<details><summary><b>Gmail & Google Calendar</b> (free)</summary>

1. Open [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen**: External, add yourself under *Test users*.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**, then download the JSON.
5. Save it as `data\google\credentials.json` and run `.\jarvis.ps1 google-login`.

In "Testing" mode Google expires the sign-in after about 7 days; run `google-login` again when JARVIS asks.
</details>

<details><summary><b>Free cloud model</b> (smarter answers when online)</summary>

Get a free key from [Groq](https://console.groq.com/keys) (or Google AI Studio / OpenRouter), then:

```powershell
.\jarvis.ps1 set-key groq
```

Set `llm.mode: cloud_first` in `config/config.yaml` (or leave `local` and the cloud is used only in gaming mode).
Free tiers have rate limits; JARVIS falls back to the local model automatically. Files, email and memory
summaries are never sent to the cloud, and `/private` forces everything local.
</details>

<details><summary><b>Home Assistant</b></summary>

Create a long-lived access token in Home Assistant (Profile → Security), run
`.\jarvis.ps1 set-key home_assistant`, and set `home_assistant.url` in `config/config.yaml`.
</details>

## Configure

- `config/config.yaml`: models, voice, wake word, allowed folders, app allowlist, games, briefing, HUD, cloud
- `config/persona.md` and `config/personas/*.md`: personality
- Runtime data (memory, logs, models, Google token) lives in `data/`, which git ignores.

## Project structure

```
config/            settings, persona, personality profiles
src/jarvis/
  core/            config, orchestrator, gaming mode, proactive alerts
  llm/             Ollama brain, cloud brain + router, tool-group selection
  tools/           tool registry + permission gate; system, app, file, web, memory, shell tools
  audio/ stt/ tts/ microphone + VAD, wake word, Whisper, Piper / SAPI
  memory/          SQLite store (facts, history, summaries, audit log)
  scheduler/       reminders, tasks, briefing
  integrations/    Google (Gmail/Calendar), Home Assistant
  ui/              HUD (FastAPI + web page), tray icon
tests/
start-jarvis.ps1   start script (+ start-jarvis.cmd for double-click)
kill-jarvis.ps1    emergency stop (+ kill-jarvis.cmd)
jarvis.ps1         all other control commands
```

## Development

```powershell
uv run python -m jarvis --debug    # run with debug logs
.\jarvis.ps1 test                  # tests + lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch model (`main` / `develop` / `feature/*`) and commit conventions.

> **Windows Smart App Control:** if it's on, it blocks unsigned native code, including uv's own Python builds,
> the `.exe` launchers uv generates and some compiled packages (e.g. SQLAlchemy). This project uses the signed
> python.org Python (`python-preference = "only-system"`), avoids the blocked packages, and runs everything
> with `uv run python -m ...` or `jarvis.ps1`.

## License

[MIT](LICENSE)
