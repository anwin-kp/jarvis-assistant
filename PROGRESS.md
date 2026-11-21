# JARVIS progress & context (read this first)

Compact state for continuing work without re-reading the codebase. Update it with every merged change.

## Map
| Area | Where | Notes |
|---|---|---|
| Entry / app loop | `src/jarvis/__main__.py` (`App`) | voice turns, HUD events, approvals, interrupts, learner start |
| Wiring | `src/jarvis/app.py` (`build`) | registers all tools, brains, playbooks |
| Turn logic | `core/orchestrator.py` | system prompt (persona + `<<personal>>` block + playbook), routing flags |
| Brains | `llm/ollama_brain.py` (local qwen3:8b), `llm/cloud_brain.py` (Router, CloudChain Groq→Gemini) | smart mode: actions + hard questions → cloud; private groups stay local |
| Tool selection | `llm/tool_groups.py` (keywords), `llm/semantic.py` | eval: `.\jarvis.ps1 eval` (85 cases, 100%) |
| Tools | `tools/*.py`, `integrations/spotify.py`, `agent/operator.py` (operate_app) | COM tools run on `agent/uia.py` thread; approvals: `security.no_approval` |
| Memory | `memory/store.py`, `memory/learner.py` | facts/lessons learned while idle, evidence-checked |
| Playbooks | `tasks/playbooks.py`, `knowledge/tasks/*.yaml` | retrieved per turn; guarded learning |
| Voice | `audio/engine.py` (VAD, barge-in, 600 ms pre-roll, short-word lane), `stt/` (Groq turbo + names) | `.\jarvis.ps1 voice-test` |
| HUD | `ui/hud.py`, `ui/web/index.html` | attachments, memory panel, cut-in |
| Overlay app | branch `feature/desktop-overlay` only: `ui/overlay.py` | PySide6; never merge into develop (draft PR #42) |
| Health | `.\jarvis.ps1 review` (failed turns), `data/logs/jarvis.log` | private data in `data/` is never committed |

## Workflow
- **Two folders.** `C:\Developmnet\Jarvis` = the user's running app, always on `feature/desktop-overlay` (never switch it).
  `C:\Developmnet\Jarvis-dev` = git worktree for development (`data` is a junction to the app's data).
  In the dev folder run Python as `PYTHONPATH=<dev>/src ../Jarvis/.venv/Scripts/python.exe ...` (the venv's editable
  install points at the app folder).
- Dev: branch from `develop` → PR → Windows CI → squash merge. Then in the app folder: `git pull`,
  `git merge develop` into `feature/desktop-overlay`, push, `jarvis.ps1 restart`.
- Before merging: `ruff check src tests`, `pytest -q`; eval when prompts/routing/tools change.
- Smart App Control: only signed native code (check imports of new deps).
- Patch files via a script file (not bash heredocs: they mangle `\b` and `\n`).

## Done (PRs)
#26–#41 voice engine, Spotify, vision, operator, HUD attachments, location, cut-in, memory/learner ·
#43 cloud actions + fallback · #44 verified actions · #45 review + voice fixes · #46 lessons + plan-then-do ·
#47 Gemini flash-lite · #48 voice test + 600 ms pre-roll · #49 short words · #50 task playbooks · #51 read-only tools don't back claims · #52 email modify ·
overlay branch: transparent overlay, Ctrl+Alt+J/H/D, auto-hide over fullscreen/gaming.

## Backlog (highest impact first)
1. Re-run `review` after recent fixes; fix the top new failure classes.
2. Overlay extras (user-optional): live captions, tray controls, position/size settings.
3. Remaining eval miss class: in-app tasks routed locally (privacy) sometimes `open_app` first.

## Known issues
- Groq free tier: 8k tokens/min per model; operator screenshots are ~4k tokens (text-first mitigates).
- Windows location is IP-based (~20 km) on this PC; `location.home` overrides.
