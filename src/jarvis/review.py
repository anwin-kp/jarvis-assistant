"""Find the turns that went wrong in real use, so they become test cases: `python -m jarvis.review [--days 7]`.

Signals per turn (a user message and what followed it):
  tool_error   a tool failed, was invalid or was declined
  nudged       the reply claimed/promised an action no tool backed up
  empty        "(no reply)" or a reply that only apologises / says it couldn't
  corrected    the next message corrects it ("no", "I meant", "that's wrong", "why did you"...)
  repeated     the next message (within 3 min) says nearly the same thing again
  stopped      the user interrupted it
The report (data/review/) contains your conversations: it stays on this PC and is never committed."""

import argparse
import datetime as dt
import difflib
import re
import sqlite3
from pathlib import Path

from jarvis.core.config import DATA

LOG = DATA / "logs" / "jarvis.log"
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
CORRECTION = re.compile(r"^(no\b|nope|not that|i (said|meant|asked)|that'?s (not|wrong)|wrong|why (did|are|is) you|"
                        r"you (didn'?t|did not)|it (didn'?t|did not|is not|isn'?t))", re.I)
APOLOGY = re.compile(r"^(i'?m sorry|sorry|i (can'?t|cannot|couldn'?t|was unable)|unfortunately)", re.I)


def _events(since: dt.datetime) -> list[tuple[dt.datetime, str]]:
    out = []
    if not LOG.exists():
        return out
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.match(line)
        if not m:
            continue
        t = dt.datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S")
        if t < since:
            continue
        if "tool=" in line and "status=" in line:
            tool, status = re.search(r"tool=(\w+) status=(\w+)", line).groups()
            out.append((t, f"{tool}:{status}"))
        elif "nudging" in line:
            out.append((t, "NUDGE"))
        elif "Interrupted (" in line:
            out.append((t, "STOP"))
    return out


def turns(days: float = 7, db: Path = DATA / "jarvis.db") -> list[dict]:
    since = dt.datetime.now() - dt.timedelta(days=days)
    con = sqlite3.connect(db)
    rows = con.execute("SELECT ts, role, text FROM messages WHERE ts >= ? ORDER BY id",
                       (since.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
    events = _events(since)
    out: list[dict] = []
    for i, (ts, role, text) in enumerate(rows):
        if role != "user":
            continue
        t = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        reply = rows[i + 1][2] if i + 1 < len(rows) and rows[i + 1][1] == "assistant" else ""
        nxt = next((r for r in rows[i + 1:] if r[1] == "user"), None)
        t_next = dt.datetime.strptime(nxt[0], "%Y-%m-%d %H:%M:%S") if nxt else t + dt.timedelta(minutes=10)
        ev = [e for when, e in events if t - dt.timedelta(seconds=2) <= when < t_next]
        tools = [e for e in ev if ":" in e]
        signals = []
        if any(not e.endswith(":ok") for e in tools):
            signals.append("tool_error")
        if "NUDGE" in ev:
            signals.append("nudged")
        if not reply.strip() or reply.strip() == "(no reply)" or APOLOGY.match(reply.strip()):
            signals.append("empty")
        if "STOP" in ev:
            signals.append("stopped")
        if nxt and (t_next - t).total_seconds() < 180:
            if CORRECTION.match(nxt[2].strip()):
                signals.append("corrected")
            elif difflib.SequenceMatcher(None, text.lower(), nxt[2].lower()).ratio() > 0.8:
                signals.append("repeated")
        out.append({"ts": ts, "user": text, "reply": reply, "tools": tools, "signals": signals,
                    "next": nxt[2] if nxt else ""})
    return out


def report(days: float = 7) -> Path:
    all_turns = turns(days)
    bad = [t for t in all_turns if t["signals"]]
    folder = DATA / "review"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"review-{dt.date.today():%Y%m%d}.md"
    counts: dict[str, int] = {}
    for t in bad:
        for s in t["signals"]:
            counts[s] = counts.get(s, 0) + 1
    lines = [f"# JARVIS review: last {days:g} days", "",
             f"{len(all_turns)} turns, {len(bad)} flagged ({100 * len(bad) / max(1, len(all_turns)):.0f}%): "
             + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])), ""]
    for t in bad:
        lines += [f"## {t['ts']}  [{', '.join(t['signals'])}]", f"- **You:** {t['user'][:300]}",
                  f"- **JARVIS:** {t['reply'][:300]}", f"- **Tools:** {', '.join(t['tools']) or 'none'}"]
        if t["next"]:
            lines.append(f"- **Next:** {t['next'][:200]}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    write_candidates(all_turns, folder / f"eval-candidates-{dt.date.today():%Y%m%d}.yaml")
    return path


# ------------------------------------------------------------------ failures -> eval cases (the reliability loop)
MY_CASES = DATA / "evals" / "my_cases.yaml"  # personal eval cases: your own words, so never in the public repo
WORTH = {"corrected", "repeated", "empty", "tool_error", "nudged"}


def candidates(all_turns: list[dict]) -> list[dict]:
    """Flagged turns as eval cases to review: what you said, what JARVIS used, why it was flagged.
    'expect' starts as '?': you write the tool(s) it should have used (or 'none' for a plain answer)."""
    out, seen = [], set()
    for t in all_turns:
        say = " ".join(t["user"].split())
        if not set(t["signals"]) & WORTH or len(say.split()) < 2 or say.lower() in seen or len(say) > 200:
            continue
        seen.add(say.lower())
        out.append({"say": say, "expect": ["?"], "got": [e.split(":")[0] for e in t["tools"]] or ["none"],
                    "why": sorted(set(t["signals"]) - {"stopped"}), "jarvis": " ".join(t["reply"].split())[:140]})
    return out


def write_candidates(all_turns: list[dict], path: Path) -> int:
    import yaml

    cases = candidates(all_turns)
    if not cases:
        return 0
    head = ("# Turns that went wrong this week, as eval cases. For each one you want JARVIS to get right:\n"
            "#   replace '?' in expect with the tool(s) it should have used (or none), then run\n"
            "#   .\\jarvis.ps1 eval-add   (adds them to your personal eval in data/evals; never committed)\n"
            "# Leave '?' to skip a case.\n")
    path.write_text(head + yaml.safe_dump(cases, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return len(cases)


def add_cases(path: Path | None = None) -> tuple[int, Path | None]:
    """Reviewed candidates (expect filled in) -> data/evals/my_cases.yaml. Returns (added, file read)."""
    import yaml

    path = path or max((DATA / "review").glob("eval-candidates-*.yaml"), default=None)
    if not path or not path.exists():
        return 0, None
    reviewed = [c for c in yaml.safe_load(path.read_text(encoding="utf-8")) or []
                if c.get("say") and c.get("expect") and "?" not in c["expect"]]
    mine = yaml.safe_load(MY_CASES.read_text(encoding="utf-8")) if MY_CASES.exists() else []
    known = {c["say"].lower() for c in mine or []}
    new = [{"say": c["say"], "expect": [str(e) for e in c["expect"]]}
           for c in reviewed if c["say"].lower() not in known]
    if new:
        MY_CASES.parent.mkdir(parents=True, exist_ok=True)
        MY_CASES.write_text(yaml.safe_dump((mine or []) + new, sort_keys=False, allow_unicode=True, width=120),
                            encoding="utf-8")
    return len(new), path


TARGET = 15  # % of turns that go wrong (interruptions aside): the reliability goal (JARVIS_PLAN phase 11)
HISTORY = DATA / "review" / "history.json"


def record_week(turn_count: int, flagged: int) -> str:
    """Keep the weekly failure rate; returns ' (target < 15%; last week 22%)'."""
    import json

    pct = round(100 * flagged / max(1, turn_count))
    try:
        hist = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    prev = hist[-1]["flagged_pct"] if hist else None
    hist.append({"date": dt.date.today().isoformat(), "turns": turn_count, "flagged_pct": pct})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist[-52:]), encoding="utf-8")
    return f" ({pct}%, target < {TARGET}%" + (f"; last week {prev}%" if prev is not None else "") + ")"


LAST_RUN = DATA / "review" / "last-run.txt"


def run_weekly(every_days: float = 7) -> str | None:
    """The automatic weekly self-check: at most once every `every_days`, write the report and return a one-line
    summary for the HUD (None when not due or nothing to report)."""
    import time

    try:
        if time.time() - float(LAST_RUN.read_text(encoding="utf-8")) < every_days * 86400:
            return None
    except (OSError, ValueError):
        pass
    all_turns = turns(every_days)
    LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(str(time.time()), encoding="utf-8")
    if not all_turns:
        return None
    path = report(every_days)
    counts: dict[str, int] = {}
    for t in all_turns:
        for s in set(t["signals"]) - {"stopped"}:  # being talked over is normal, not a failure
            counts[s] = counts.get(s, 0) + 1
    flagged = sum(1 for t in all_turns if set(t["signals"]) - {"stopped"})
    top = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:3])
    trend = record_week(len(all_turns), flagged)
    return (f"Weekly self-check: {len(all_turns)} turns, {flagged} went wrong{trend}" + (f": {top}" if top else "")
            + f". Report: {path.relative_to(DATA.parent)}. Turn them into tests: .\\jarvis.ps1 eval-add")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--add", nargs="?", const="", help="add reviewed eval candidates to your personal eval")
    a = ap.parse_args()
    if a.add is not None:
        n, src = add_cases(Path(a.add) if a.add else None)
        print(f"Added {n} case(s) from {src} to {MY_CASES}" if src
              else "No eval-candidates file yet: run review first.")
    else:
        out = report(a.days)
        print(f"Report: {out}")
        print(out.read_text(encoding="utf-8").splitlines()[2])
        cands = max(out.parent.glob("eval-candidates-*.yaml"), default=None)
        if cands:
            print(f"Eval candidates: {cands} (fill in 'expect', then: .\\jarvis.ps1 eval-add)")
