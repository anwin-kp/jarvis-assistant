"""Learn about the user from their own accounts (on this PC only; nothing is sent anywhere new).

- Spotify: favourite artists from Liked Songs, and playlist names.
- Gmail: names of the people the user writes to most (Sent mail headers only, never message bodies).
- Calendar: regular events (titles that repeat) and the people in them.

The names feed the speech vocabulary (stt/lexicon.py); a few summary facts go into memory, refreshed in place.
Runs in the background at most once a day.
"""

from __future__ import annotations

import collections
import json
import re
import time
from datetime import datetime, timedelta
from email.utils import getaddresses
from pathlib import Path

from loguru import logger

REFRESH_HOURS = 24


def spotify_taste(sp, max_tracks: int = 500) -> tuple[list[str], list[str]]:
    """(favourite artists by number of liked songs, playlist names)."""
    artists: collections.Counter = collections.Counter()
    url = "/me/tracks?limit=50"
    seen = 0
    while url and seen < max_tracks:
        page = sp._call("GET", url)
        for item in page.get("items", []):
            for a in (item.get("track") or {}).get("artists", []):
                if a.get("name"):
                    artists[a["name"]] += 1
        seen += len(page.get("items", []))
        nxt = page.get("next")
        url = nxt.split("/v1", 1)[-1] if nxt else None
    playlists = [p["name"] for p in sp.my_playlists() if p.get("name")]
    return [a for a, _ in artists.most_common(40)], playlists


def _clean_name(name: str) -> str | None:
    name = re.sub(r"\s*\(.*?\)|\s*<.*?>|[\"']", "", name).strip()
    if not name or "@" in name or len(name) > 40 or not re.search(r"[A-Za-z]", name):
        return None
    if re.search(r"\b(no-?reply|team|support|notifications?|newsletter|info|admin)\b", name, re.I):
        return None
    return name


def gmail_people(service, own_names: set[str], limit: int = 150) -> collections.Counter:
    """People the user writes to most: To/Cc names of their sent mail (headers only)."""
    ids = service.users().messages().list(userId="me", q="in:sent newer_than:1y", maxResults=limit).execute()
    count: collections.Counter = collections.Counter()
    for m in ids.get("messages", []):
        msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["To", "Cc"]).execute()
        heads = [h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() in ("to", "cc")]
        for name, _addr in getaddresses(heads):
            if (n := _clean_name(name)) and n.lower() not in own_names:
                count[n] += 1
    return count


def calendar_regulars(service, own_names: set[str]) -> tuple[list[str], list[str]]:
    """(event titles that repeat in the last 90 days / next 30, people who attend them)."""
    now = datetime.now().astimezone()
    items = service.events().list(calendarId="primary", timeMin=(now - timedelta(days=90)).isoformat(),
                                  timeMax=(now + timedelta(days=30)).isoformat(), singleEvents=True,
                                  maxResults=500).execute().get("items", [])
    titles = collections.Counter(e.get("summary", "").strip() for e in items if e.get("summary"))
    people: collections.Counter = collections.Counter()
    for e in items:
        for a in e.get("attendees", []):
            if (n := _clean_name(a.get("displayName", ""))) and not a.get("self") and n.lower() not in own_names:
                people[n] += 1
    return [t for t, c in titles.most_common(15) if c >= 2], [n for n, _ in people.most_common(20)]


def _upsert_fact(memory, prefix: str, fact: str) -> None:
    """One fact per topic, replaced when it changes (never a pile of near-duplicates)."""
    old = next((f for f in memory.list_facts() if f["fact"].startswith(prefix)), None)
    if old and old["fact"] != fact:
        memory.replace_fact(old["id"], fact)
    elif not old:
        memory.learn(fact)


def refresh(memory, cache: Path, spotify=None, google_ok: bool = False, own_names: set[str] | None = None,
            force: bool = False) -> dict:
    """Gather names from the user's accounts. Returns {"artist": [...], "playlist": [...], "person": [...],
    "event": [...]} (also cached in `cache`, so a restart doesn't need the network)."""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not force and time.time() - data.get("at", 0) < REFRESH_HOURS * 3600:
        return data
    own = {n.lower() for n in (own_names or set())}
    if spotify:
        try:
            artists, playlists = spotify_taste(spotify)
            data.update(artist=artists, playlist=playlists)
            if artists:
                _upsert_fact(memory, "[music] Favourite artists",
                             "[music] Favourite artists (from Spotify Liked Songs): " + ", ".join(artists[:8]) + ".")
        except Exception as e:
            logger.debug("Spotify taste: {}", e)
    if google_ok:
        try:
            from jarvis.integrations.google import _service

            sent = gmail_people(_service("gmail", "v1"), own)
            events, attendees = calendar_regulars(_service("calendar", "v3"), own)
            data.update(person=list(dict.fromkeys([n for n, _ in sent.most_common(30)] + attendees)), event=events)
            if regulars := [n for n, c in sent.most_common(8) if c >= 2]:  # one email isn't "emails most"
                _upsert_fact(memory, "[people] Emails most with",
                             "[people] Emails most with: " + ", ".join(regulars) + ".")
            if events:
                _upsert_fact(memory, "[work] Regular calendar events",
                             "[work] Regular calendar events: " + ", ".join(events[:6]) + ".")
        except Exception as e:
            logger.debug("Gmail/Calendar names: {}", e)
    data["at"] = time.time()
    try:
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    logger.info("Personal data: {} artists, {} playlists, {} people, {} regular events",
                *(len(data.get(k, [])) for k in ("artist", "playlist", "person", "event")))
    return data
