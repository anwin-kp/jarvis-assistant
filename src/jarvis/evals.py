"""Tool-selection eval:  .\\jarvis.ps1 eval   (or: python -m jarvis.evals [--fast-only])
Runs every case in tests/evals/tool_selection.yaml through the real local model in dry-run mode (nothing on
the PC changes), with a throwaway memory database, and reports accuracy and latency."""

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

import yaml
from loguru import logger

CASES = Path(__file__).resolve().parents[2] / "tests" / "evals" / "tool_selection.yaml"


def run(limit: int | None = None, verbose: bool = False, use_fast: bool = True, local_only: bool = False) -> dict:
    import jarvis.app as app

    logger.remove()
    app.DATA = Path(tempfile.mkdtemp())
    from jarvis.tasks import playbooks  # test traffic must never teach JARVIS: learn into the temp folder

    playbooks.LEARNED, playbooks.NOTES = app.DATA / "tasks" / "learned", app.DATA / "tasks" / "notes"
    cfg, orch, brain, reg, memory, _ = app.build(dry_run=True)
    if local_only and hasattr(brain, "cloud"):  # "the cloud is unreachable": everything on the local model
        brain.cloud = None
    from jarvis.core.fast_intents import FastIntents

    fast = FastIntents(reg, cfg)
    calls: list[str] = []
    reg.on_call = lambda name, args, status: calls.append(name)
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))[:limit]
    passed, times, fails, fast_hits = 0, [], [], 0
    for c in cases:
        calls.clear()
        orch.reset()
        t = time.perf_counter()
        quick = fast.try_handle(c["say"], lambda tool, args: True) if use_fast else None
        if quick is None:
            orch.handle(c["say"], lambda tool, args: True)
        else:
            fast_hits += 1
        dt = time.perf_counter() - t
        times.append(dt)
        got = calls[0] if calls else "none"
        ok = got in c["expect"]
        passed += ok
        if not ok:
            fails.append(f"  ✗ {c['say']!r}: got {got}, expected {c['expect']}")
        if verbose:
            print(f"{'✓' if ok else '✗'} {dt:4.1f}s {'[fast] ' if quick is not None else ''}{c['say']} -> {got}")
    result = {"cases": len(cases), "passed": passed, "accuracy": round(100 * passed / len(cases), 1),
              "median_s": round(statistics.median(times), 2), "p90_s": round(sorted(times)[int(len(times) * .9)], 2),
              "fast_path": fast_hits}
    print("\n".join(fails))
    print(f"\nAccuracy {result['accuracy']}% ({passed}/{len(cases)}) · median {result['median_s']}s · "
          f"p90 {result['p90_s']}s · instant commands {fast_hits}")
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-fast", action="store_true", help="disable instant commands (baseline)")
    ap.add_argument("--local", action="store_true", help="cloud unreachable: local model only")
    a = ap.parse_args()
    run(a.limit, a.verbose, not a.no_fast, a.local)
