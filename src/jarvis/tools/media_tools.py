"""Media control: play/pause/next via media keys, play on YouTube / search on Spotify."""

import ctypes
import os
import re
import time
import webbrowser
from urllib.parse import quote_plus

import requests

from jarvis.tools.registry import ToolRegistry

KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
        "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}
ACTIONS = ["play", "pause", *KEYS]  # play / pause check what's playing first (the media key only toggles)
STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en"}


def press_key(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def _app_name(aumid: str) -> str:
    """'Spotify.exe' / 'SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify' / 'MSEdge' -> a readable app name."""
    name = re.split(r"[!\\]", aumid or "")[-1].removesuffix(".exe")
    return {"msedge": "Edge", "chrome": "Chrome"}.get(name.lower(), name) or "an app"


def media_sessions(command: str | None = None) -> list[dict] | None:
    """What's playing in every app that reports to Windows (Spotify, browsers, players), via the system media
    controls; the playing one first. command 'play' / 'pause' is sent to the first session. None = unavailable
    (then only the play/pause media key, which toggles blindly, can be used)."""
    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except Exception:
        return None

    async def go():
        mgr = await Manager.request_async()
        out = []
        for s in mgr.get_sessions():
            try:
                props = await asyncio.wait_for(s.try_get_media_properties_async(), 2)
                title, artist = props.title or "", props.artist or ""
            except Exception:
                title = artist = ""
            status = STATUS.get(int(s.get_playback_info().playback_status), "unknown")
            out.append({"app": _app_name(s.source_app_user_model_id), "status": status, "title": title,
                        "artist": artist, "_s": s})
        out.sort(key=lambda x: x["status"] != "playing")
        if command and out:
            s = out[0].pop("_s")
            ok = await (s.try_play_async() if command == "play" else s.try_pause_async())
            out[0]["sent"] = bool(ok)
        for x in out:
            x.pop("_s", None)
        return out

    try:  # WinRT on its own thread: a thread has one COM mode, and pycaw/pywinauto need the other
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(asyncio.wait_for(go(), 4))).result()
    except Exception:
        return None


def _what(x: dict) -> str:
    song = " by ".join(p for p in (x.get("title"), x.get("artist")) if p)
    return f"{song} ({x['app']})" if song else x["app"]


def play_or_pause(action: str, sessions=media_sessions) -> str:
    """'pause' when nothing plays used to START music (the media key toggles); 'play' while playing paused it."""
    found = sessions()
    if found is None:  # system media controls unavailable: the toggle key is all there is
        press_key(KEYS["play_pause"])
        return "Paused." if action == "pause" else "Playing."
    playing = [x for x in found if x["status"] == "playing"]
    if action == "pause":
        if not playing:
            return "Nothing is playing."
        done = sessions("pause")
        if not (done and done[0].get("sent")):
            press_key(KEYS["play_pause"])
        return f"Paused {_what(playing[0])}."
    if playing:
        return f"Already playing {_what(playing[0])}."
    done = sessions("play") if found else None
    if not (done and done[0].get("sent")):
        press_key(KEYS["play_pause"])  # no player open: the key wakes the last one (e.g. Spotify)
    return f"Playing {_what(found[0])}." if found else "Playing."


def first_youtube_video(html: str) -> str | None:
    m = re.search(r'"videoId":"([\w-]{11})"', html)
    return m.group(1) if m else None


def register(reg: ToolRegistry) -> None:
    @reg.tool("media_control", "Control whatever is playing (Spotify, YouTube, any player): play, pause, next, "
              "previous, stop, volume_up, volume_down, mute (play_pause toggles).",
              {"action": {"type": "string", "enum": ACTIONS}}, group="media")
    def media_control(action: str):
        if action in ("play", "pause"):
            return play_or_pause(action)
        if action not in KEYS:
            return f"ERROR: unknown action {action!r}; use one of {', '.join(ACTIONS)}."
        press_key(KEYS[action])
        return f"Sent {action.replace('_', ' ')}."

    @reg.tool("now_playing", "What's playing right now in any app (song, artist, app, playing or paused).", {},
              group="media", read_only=True)
    def now_playing():
        found = media_sessions()
        if found is None:
            return "ERROR: I can't see what's playing on this PC (Windows media controls unavailable)."
        if not found:
            return "Nothing is playing."
        return "; ".join(f"{x['status'].capitalize()}: {_what(x)}" for x in found[:4])

    @reg.tool("play_youtube", "Play a song or video on YouTube (opens and starts the top result).",
              {"query": {"type": "string"}}, group="media")
    def play_youtube(query: str):
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        try:
            vid = first_youtube_video(requests.get(url, headers=UA, timeout=10).text)
        except requests.RequestException:
            vid = None
        webbrowser.open(f"https://www.youtube.com/watch?v={vid}" if vid else url)
        return f"Playing '{query}' on YouTube." if vid else f"Opened YouTube results for '{query}'."

    @reg.tool("search_spotify", "Open Spotify and search for a song, artist, album or playlist.",
              {"query": {"type": "string"}}, group="media")
    def search_spotify(query: str):
        os.startfile(f"spotify:search:{quote_plus(query).replace('+', '%20')}")
        return f"Opened Spotify search for '{query}'. Say 'play' to start playback once you pick it."
