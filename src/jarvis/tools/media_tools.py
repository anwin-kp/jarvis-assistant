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
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en"}


def press_key(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def first_youtube_video(html: str) -> str | None:
    m = re.search(r'"videoId":"([\w-]{11})"', html)
    return m.group(1) if m else None


def register(reg: ToolRegistry) -> None:
    @reg.tool("media_control", "Control whatever is playing (Spotify, YouTube, any player): play_pause, next, "
              "previous, stop, volume_up, volume_down, mute.",
              {"action": {"type": "string", "enum": list(KEYS)}}, group="media")
    def media_control(action: str):
        press_key(KEYS[action])
        return f"Sent {action.replace('_', ' ')}."

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
