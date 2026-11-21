"""Routine learning: things you do at about the same time on several different days ("you open Spotify around 9 on
weekdays"). JARVIS only *mentions* a routine (weekly summary); it never acts on one by itself."""

import datetime as dt
import json
import statistics

# what counts as doing something (tool -> the argument that says what), from the audit log of real tool calls
ROUTINE_TOOLS = {"open_app": "name", "spotify_play": "query", "play_youtube": "query", "get_weather": None,
                 "news_headlines": None, "list_unread_emails": None, "list_events": None}
LABEL = {"open_app": "open {}", "spotify_play": "play {} on Spotify", "play_youtube": "play {} on YouTube",
         "get_weather": "check the weather", "news_headlines": "check the news",
         "list_unread_emails": "check your email", "list_events": "check your calendar"}


def find_routines(rows: list[tuple[str, str, str, str]], min_days: int = 3, window_min: int = 45) -> list[dict]:
    """rows: (ts 'YYYY-MM-DD HH:MM:SS', tool, args json, status). A routine = the same action on >= min_days
    different days, within +-window_min of the same time of day."""
    acts: dict[tuple, list[dt.datetime]] = {}
    for ts, tool, args, status in rows:
        if tool not in ROUTINE_TOOLS or status != "ok":
            continue
        what = ""
        if key := ROUTINE_TOOLS[tool]:
            try:
                what = str(json.loads(args or "{}").get(key, "")).strip().lower().removesuffix(".exe")
            except ValueError:
                continue
        acts.setdefault((tool, what), []).append(dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
    found = []
    for (tool, what), times in acts.items():
        best: list[dt.datetime] = []
        for t in times:  # the time of day that the most different days share
            mins = t.hour * 60 + t.minute
            near = [u for u in times if abs((u.hour * 60 + u.minute) - mins) <= window_min]
            days = {u.date() for u in near}
            if len(days) > len({u.date() for u in best}):
                best = near
        days = sorted({u.date() for u in best})
        if len(days) < min_days:
            continue
        at = int(statistics.median(u.hour * 60 + u.minute for u in best))
        weekdays = all(d.weekday() < 5 for d in days)
        found.append({"tool": tool, "what": what, "time": f"{at // 60:02d}:{at % 60:02d}", "days": len(days),
                      "weekdays": weekdays,
                      "text": f"{LABEL[tool].format(what.title() if what else '')} around {at // 60}:{at % 60:02d}"
                              f"{' on weekdays' if weekdays else ''} ({len(days)} days)"})
    return sorted(found, key=lambda r: -r["days"])


def weekly_note(db_path, days: int = 28) -> str:
    """One line for the weekly summary, or '' (read-only: the audit log of the last `days` days)."""
    import sqlite3

    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT ts, tool, args, status FROM audit_log WHERE ts >= ?", (since,)).fetchall()
    finally:
        con.close()
    found = find_routines(rows)[:3]
    return ("Routines I noticed: you " + "; ".join(r["text"] for r in found)
            + ". Want reminders for any of them? Just ask.") if found else ""
