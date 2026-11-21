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
    return path


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
    return (f"Weekly self-check: {len(all_turns)} turns, {flagged} went wrong" + (f" ({top})" if top else "")
            + f". Report: {path.relative_to(DATA.parent)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    out = report(ap.parse_args().days)
    print(f"Report: {out}")
    print(out.read_text(encoding="utf-8").splitlines()[2])
