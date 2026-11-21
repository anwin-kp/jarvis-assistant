# Task playbooks

How JARVIS does PC tasks correctly. One YAML file per task. JARVIS finds the right one by meaning before acting,
and puts it in the AI's context: "follow this playbook, then verify as it says".

```yaml
id: set-volume                      # lowercase-with-dashes, same as the file name
title: Change or mute the PC volume
status: verified                    # verified | candidate (learned once, not injected automatically)
source: curated                     # curated (this folder, reviewed) | learned (data/tasks/learned, private)
triggers:                           # how people actually ask: these are what retrieval matches against
  - set the volume to 30
  - it's too loud
what: Sets the Windows speaker volume or mute.          # purpose and expected outcome
steps:                                                  # exact tool calls, in order
  - "absolute: set_volume(percent=N)"
tools: [set_volume, get_volume, set_mute]               # every tool must exist and appear in steps (tested in CI)
verify:                                                 # how to confirm it really happened
  - set_volume's result says '(checked)'
errors:                                                 # common failures and recovery
  - symptom: "WARNING: asked for X% but the volume is Y%"
    recovery: tell the user; another app may control volume
safety: safe, no approval.          # risks; cards with dangerous tools must say DANGER and "approval" (tested)
```

Rules:
- **A card guides, it never grants.** Every action still goes through the tool, its approval and its result check.
- **Curated cards** are reviewed like code; `tests/test_playbooks.py` validates every file against the real tools.
- **Learned cards** (`data/tasks/learned/`, private) come only from clean multi-step successes that the user didn't
  correct: no failed, declined or warned runs, no dangerous tools. They become `verified` after succeeding twice.
- **Corrections** given right after a playbook was used are saved as notes on that card (`data/tasks/notes/`) and
  shown to the AI with it ("always follow").
- See and manage: `.\jarvis.ps1 tasks`, `.\jarvis.ps1 tasks show <id>`, `.\jarvis.ps1 tasks forget <id>`.
