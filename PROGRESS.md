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

- **Missions** (`agent/missions.py`): goal -> checked plan (call / work / write steps) -> background worker -> review.
  State in `data/missions/*.json` (saved after each change; resumes after a restart). Policy in code: `QUIET` changes
  run, every other change waits for approval; re-plans can't add risky tools; private data stays on the local model;
  `ASKED` / `INTENT` tie changes to the user's own words. Eval: `python -m jarvis.mission_eval` (real models, fake tools).
- **Developer loop** (`agent/coder.py`): checks before -> fix -> checks after -> review by another model -> a bug's new
  test must fail without the fix; up to 3 rounds. `verified` is decided in code. `open_pull_request` (draft, asks).
  Missions use it through `fix_code` (group "background": never offered in conversation). Eval: `python -m jarvis.code_eval`.

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
#47 Gemini flash-lite · #48 voice test + 600 ms pre-roll · #49 short words · #50 task playbooks · #51 read-only tools don't back claims · #52 email modify · #54 Spotify retry + cloud handover · #55 smaller cloud requests · #56 offline tasks (local eval, offline Spotify) · #57 distant speech kept · #58 weekly self-check · #59 live captions · #60 briefing when you sit down · #61 conversation survives restarts · #63-#67 smoke-test fixes (self-echo, gated-mic echo, operator Gemini fallback, list_processes) · #69 close_app really closes (real process names, WM_CLOSE + verify, no false "closed") · #70 speed: 2 more Groq models before Gemini (own 8k TPM each), per-model cooldown from retry-after, Gemini 10 s cap, short commands not dropped as echo, eval --cloud · #71 misheard app names ("closed not padder" -> close notepad; sound-alike match, app names in STT vocabulary) · #72 cloud requests 20-33% smaller (only examples for the offered tools) · #73 offline: last known weather/location/news/email/calendar with its age (email/calendar RAM only) · #74 document index skips app/game caches and word lists · #75 personal vocabulary: ranked names (said/misheard first) within Whisper's limit, after-recognition name correction, Spotify artists + Gmail/Calendar names (private names local-only until said) · #76 learn from a whole document about you (attach + "learn about me": LinkedIn PDF, CV) · #77 audit: "stop" ends multi-step turns (local + cloud), approvals keep queued events, play/pause check what's playing (Windows media controls; new now_playing), Ollama down -> cloud, empty reply after tools says the result, Groq 400 no cooldown, RSS timeouts, PowerShell .NET-call block, kill_process never JARVIS, fact vectors cached · #78 one-shot tools: your own Chrome/Edge tabs (list/switch/close matching|others|duplicates, each close verified, never the last tab, private = local only; instant 'keep only one tab'), calculate, convert_units, convert_currency (ECB), date_math, define_word, network_status, recent_files, open_in_browser · #79 HUD reactor capped at 60/30/12 fps by state (was the 200 Hz display rate) · #80 gauges (nvidia-smi) measured only while a window shows them · #83 SETUP.md · v1.1.0 released · #87 'mark them as read' instant (even misheard 'red') · #88 long replies spoken short + 'More on screen.' (voice.max_spoken_words; 31-60-word replies were interrupted 64%) · #89 arrange_windows (move-then-size: DPI-safe across 200%/100% monitors), clean_old_downloads (asks with details), timers show time left · plan v2 phases 11-16: #92 reliability loop (eval candidates, personal eval, release-check) · #93 cloud streaming (first sentence 0.8 s) · #94 echo canceller + recorder (not wired yet) · #95 memory slots + fact history · #96 open_app brings windows to front · #97 active-window context + routine suggestions · #98 update + doctor · #99 code projects (read/search/git/run allowlisted dev commands) · #100 coding agent (own worktree + branch, free models, merge/discard) · #101 audio devices, winget install/update, workspaces, selected text · #102 system health + bulk files · #103 offline vision (qwen3-vl:4b) + eval 99.2% ·
overlay branch: transparent overlay, Ctrl+Alt+J/H/D, auto-hide over fullscreen/gaming, position & size settings (gear), tray Overlay submenu; #81 passive overlay window = only its strip (full display only when active).

## Backlog (highest impact first)
1. Echo cancellation: set voice.record_echo, collect replies, run python -m jarvis.audio.aec; wire into barge-in
   only if >= 15 dB on real recordings.
2. Run the eval with Ollama up (#88-#97 changed tools/prompts without an eval run), then release v1.2.0.
3. Fill in data/review/eval-candidates-*.yaml and run eval-add (reliability loop).

## Testing
- Performance (idle, overlay app, 2026-11-21): JARVIS ~1.3% CPU, ~1.4 GB RAM, ~6.9 GB VRAM (Ollama qwen3:8b 6.1 GB + local Whisper 0.8 GB); median reply: instant 0.0 s, local 1.5 s, cloud 4.7 s (slow cloud turns = multi-step actions with verification waits).
- Live smoke test (14 real commands via the HUD websocket, safe/reversible): check replies, timings,
  `Barge-in accepted` / `Heard my own voice` / `status=error` counts in the log since the last restart.

## Known issues
- Groq free tier: 8k tokens/min per model (~0.9-1.4k per request after #72). Cloud requests send ~10 tools (playbook + closest by meaning) and the
  last 3 exchanges; a mid-task 429 hands over to Gemini as plain text. Operator screenshots are ~4k tokens.
- Loud Bluetooth speaker + gated headset mic: the echo can be louder than the user's voice; talking over
  JARVIS then needs a louder voice (HUD Stop always works).
- Windows location is IP-based (~20 km) on this PC; `location.home` overrides.
