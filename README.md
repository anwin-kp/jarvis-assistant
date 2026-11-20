# JARVIS Assistant

[![CI](https://github.com/anwin-kp/jarvis-assistant/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/anwin-kp/jarvis-assistant/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6)

A **free, local-first** JARVIS-style personal AI assistant for Windows. The AI runs on your own GPU
through [Ollama](https://ollama.com). No paid APIs, and it works offline.

See [JARVIS_PLAN.md](JARVIS_PLAN.md) for the full architecture and roadmap.

## Status

**v0.1.0 (Phase 1):** a text chat with tool use.

| Area | What JARVIS can do |
|---|---|
| System | CPU/RAM/disk/battery info, top processes, volume, mute, brightness, lock screen |
| Apps | Open/close apps from an allowlist |
| Files | Search, list, open, create, move, delete (to the Recycle Bin), limited to allowed folders |
| Web | DuckDuckGo search, read web pages, weather (Open-Meteo), Wikipedia, news (RSS) |
| Memory | Remembers facts about you in a local SQLite database |
| Safety | Permission tiers (safe / confirm / danger), approval prompts, audit log, dry-run mode, kill switch |

Next: Phase 2, voice (push-to-talk, Whisper speech-to-text, text-to-speech).

## Requirements

- Windows 10/11
- [Python 3.12](https://www.python.org/downloads/) (python.org build), [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com)
- NVIDIA GPU with ~6 GB+ VRAM recommended (it also runs on CPU, more slowly)

```powershell
winget install Python.Python.3.12 astral-sh.uv Ollama.Ollama
git clone https://github.com/anwin-kp/jarvis-assistant.git
cd jarvis-assistant
uv sync
ollama pull qwen3:8b
.\jarvis.ps1 start
```

## Control: `jarvis.ps1` (all commands in one file)

```powershell
.\jarvis.ps1 start            # start JARVIS in a new window (starts Ollama if needed)
.\jarvis.ps1 start -DryRun    # simulation mode: actions that change things are only simulated
.\jarvis.ps1 status           # JARVIS / Ollama / model / GPU memory
.\jarvis.ps1 free             # unload the AI model from the GPU (e.g. before gaming)
.\jarvis.ps1 stop             # close JARVIS and free the GPU
.\jarvis.ps1 restart
.\jarvis.ps1 stop-all         # close JARVIS, free the GPU and shut down Ollama
.\jarvis.ps1 logs [-Follow]   # show the log
.\jarvis.ps1 test             # unit tests + lint
```

Inside the JARVIS window: `/help`, `/reset`, `/facts`, `/tools`, `/unload`, `/quit`.

## Emergency stop: `kill-switch.ps1`

Double-click **`kill-switch.cmd`** (or run `.\kill-switch.ps1`). It force-kills all JARVIS processes,
unloads the model from GPU memory, and kills Ollama, then confirms everything stopped. It doesn't depend
on `jarvis.ps1` or Python, and every use is logged to `data/logs/killswitch.log`.

Ollama starts automatically when you log in to Windows; the kill switch stops it for the current session only.

## Configure

- `config/config.yaml`: model, allowed folders, app allowlist, news feeds
- `config/persona.md`: JARVIS's personality and rules
- Runtime data (memory, logs, audit trail) lives in `data/`, which git ignores.

## Project structure

```
config/            settings and persona
src/jarvis/
  core/            config loading, orchestrator (turn loop)
  llm/             Ollama brain, tool-group selection
  tools/           tool registry + permission gate, system/app/file/web/memory tools
  memory/          SQLite store
tests/             unit tests
jarvis.ps1         control script
kill-switch.ps1    emergency stop
```

## Development

```powershell
uv run python -m jarvis --debug    # run directly with debug logs
.\jarvis.ps1 test                  # tests + lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch model (`main` / `develop` / `feature/*`) and commit conventions.

> **Windows Smart App Control:** if it's on, it blocks uv's own unsigned Python builds and the `.exe`
> launchers uv generates (`jarvis.exe`, `pytest.exe`). This project therefore uses the signed python.org
> Python (`python-preference = "only-system"` in `pyproject.toml`) and runs everything with
> `uv run python -m ...` or `jarvis.ps1`.

## License

[MIT](LICENSE)
