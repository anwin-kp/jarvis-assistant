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
| Health | weekly self-check (auto), `.\jarvis.ps1 review`, `evals --local` (cloud off), `data/logs/jarvis.log` | private data in `data/` is never committed |

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
#47 Gemini flash-lite · #48 voice test + 600 ms pre-roll · #49 short words · #50 task playbooks · #51 read-only tools don't back claims · #52 email modify · #54 Spotify retry + cloud handover · #55 smaller cloud requests · #56 offline tasks (local eval, offline Spotify) · #57 distant speech kept · #58 weekly self-check · #59 live captions · #60 briefing when you sit down · #61 conversation survives restarts · #63-#67 smoke-test fixes (self-echo, gated-mic echo, operator Gemini fallback, list_processes) · #69 close_app really closes (real process names, WM_CLOSE + verify, no false "closed") · #70 speed: 2 more Groq models before Gemini (own 8k TPM each), per-model cooldown from retry-after, Gemini 10 s cap, short commands not dropped as echo, eval --cloud · #71 misheard app names ("closed not padder" -> close notepad; sound-alike match, app names in STT vocabulary) ·
overlay branch: transparent overlay, Ctrl+Alt+J/H/D, auto-hide over fullscreen/gaming, position & size settings (gear), tray Overlay submenu.

## Backlog (highest impact first)
1. Act on the weekly self-check summary (HUD notice + data/review/): fix the top failure classes.
2. Tools that need internet fail offline (weather, web, email): consider cached last-known answers.

## Testing
- Live smoke test (14 real commands via the HUD websocket, safe/reversible): check replies, timings,
  `Barge-in accepted` / `Heard my own voice` / `status=error` counts in the log since the last restart.

## Known issues
- Groq free tier: 8k tokens/min per model. Cloud requests send ~10 tools (playbook + closest by meaning) and the
  last 3 exchanges; a mid-task 429 hands over to Gemini as plain text. Operator screenshots are ~4k tokens.
- Loud Bluetooth speaker + gated headset mic: the echo can be louder than the user's voice; talking over
  JARVIS then needs a louder voice (HUD Stop always works).
- Windows location is IP-based (~20 km) on this PC; `location.home` overrides.
