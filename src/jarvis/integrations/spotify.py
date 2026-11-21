"""Spotify control through the official Web API (needs Spotify Premium for playback control).

Setup (one time, free): create an app at https://developer.spotify.com/dashboard with redirect URI
    http://127.0.0.1:8888/callback   (tick "Web API")
then:  .\\jarvis.ps1 set-key spotify_client_id   and   .\\jarvis.ps1 spotify-login
Login uses PKCE: no client secret is needed or stored."""

import base64
import difflib
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from loguru import logger

from jarvis.core.config import DATA, get_secret
from jarvis.tools.registry import ToolRegistry, wait_for

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read user-library-modify")
TOKEN_FILE = DATA / "spotify" / "token.json"


class SpotifyError(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def best_match(query: str, names: list[str], cutoff: float = 0.55) -> tuple[int, float] | None:
    """Fuzzy-match a spoken name ('mix up mash', 'mixup match') against e.g. your playlist names."""
    q = _norm(query)
    if not q:
        return None
    best = None
    for i, name in enumerate(names):
        n = _norm(name)
        if not n:
            continue
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if q == n:
            score = 1.0
        elif q in n or n in q:
            score = max(score, 0.85)
        if best is None or score > best[1]:
            best = (i, score)
    return best if best and best[1] >= cutoff else None


class Spotify:
    def __init__(self, client_id: str, token_file=TOKEN_FILE, session=None):
        self.client_id, self.token_file = client_id, token_file
        self.http = session or requests.Session()
        self._token = json.loads(token_file.read_text()) if token_file.exists() else None
        self._playlists: list[dict] | None = None
        self._playlists_at = 0.0

    # ------------------------------------------------------------------ auth
    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def _save(self, tok: dict) -> None:
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        if self._token and "refresh_token" not in tok:
            tok["refresh_token"] = self._token.get("refresh_token")
        self._token = tok
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok))

    def _access(self) -> str:
        if not self.logged_in:
            raise SpotifyError("Spotify isn't connected. Run: .\\jarvis.ps1 spotify-login")
        if time.time() >= self._token.get("expires_at", 0):
            r = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": self.client_id,
                                                "refresh_token": self._token["refresh_token"]}, timeout=15)
            if not r.ok:
                raise SpotifyError("Spotify login expired. Run: .\\jarvis.ps1 spotify-login")
            self._save(r.json())
        return self._token["access_token"]

    def login(self, timeout: float = 180) -> None:
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>JARVIS is connected to Spotify. You can close this tab.</h2>")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 8888), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        webbrowser.open(AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state}))
        end = time.time() + timeout
        while "code" not in result and "error" not in result and time.time() < end:
            time.sleep(0.2)
        server.server_close()
        if result.get("state") != state or "code" not in result:
            raise SpotifyError(f"Spotify login failed: {result.get('error', 'timed out')}")
        r = self.http.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": result["code"],
                                            "redirect_uri": REDIRECT, "client_id": self.client_id,
                                            "code_verifier": verifier}, timeout=15)
        if not r.ok:
            raise SpotifyError(f"Spotify login failed: {r.text[:200]}")
        self._save(r.json())

    # ------------------------------------------------------------------ API
    def _call(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, API + path, headers={"Authorization": f"Bearer {self._access()}"},
                                  timeout=15, **kw)
        except requests.ConnectionError:
            # Spotify drops idle keep-alive connections: the first call after a quiet spell failed with
            # "Remote end closed connection without response". One retry gets a fresh connection.
            r = self.http.request(method, API + path, headers={"Authorization": f"Bearer {self._access()}"},
                                  timeout=15, **kw)
        if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
            raise SpotifyError("NO_ACTIVE_DEVICE")
        if r.status_code == 403 and "PREMIUM" in r.text.upper():
            raise SpotifyError("Spotify playback control needs a Premium account.")
        if not r.ok:
            raise SpotifyError(f"Spotify error {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    def devices(self) -> list[dict]:
        return self._call("GET", "/me/player/devices").get("devices", [])

    def ensure_device(self, wait: float = 15) -> str:
        """An active device to play on: the active one, else this PC's Spotify app (opened if needed)."""
        devs = self.devices()
        active = next((d for d in devs if d.get("is_active")), None)
        if active:
            return active["id"]
        if not devs:
            os.startfile("spotify:")  # open the desktop app
            end = time.time() + wait
            while time.time() < end and not devs:
                time.sleep(1)
                devs = self.devices()
            if not devs:
                raise SpotifyError("Couldn't find a Spotify device. Is the Spotify app signed in?")
        dev = next((d for d in devs if d.get("type") == "Computer"), devs[0])
        self._call("PUT", "/me/player", json={"device_ids": [dev["id"]], "play": False})
        return dev["id"]

    def play(self, context_uri: str | None = None, uris: list[str] | None = None) -> None:
        body = {"context_uri": context_uri} if context_uri else {"uris": uris} if uris else {}
        device = self.ensure_device()
        self._call("PUT", f"/me/player/play?device_id={device}", json=body)

    def search(self, query: str, kind: str, limit: int = 5) -> list[dict]:
        res = self._call("GET", "/search", params={"q": query, "type": kind, "limit": limit})
        return [x for x in res.get(kind + "s", {}).get("items", []) if x]

    def my_playlists(self) -> list[dict]:
        if self._playlists is None or time.time() - self._playlists_at > 600:
            items, url = [], "/me/playlists?limit=50"
            try:
                while url and len(items) < 500:
                    page = self._call("GET", url)
                    items += [p for p in page.get("items", []) if p]
                    nxt = page.get("next")
                    url = nxt.replace(API, "") if nxt else None
            except requests.RequestException:
                if cached := saved_playlists():  # offline: the list saved last time
                    return cached
                raise
            self._playlists, self._playlists_at = items, time.time()
            save_playlists(items)
        return self._playlists

    def now_playing(self) -> dict | None:
        return self._call("GET", "/me/player/currently-playing") or None


PLAYLISTS_FILE = DATA / "spotify-playlists.json"


def save_playlists(items: list[dict]) -> None:
    """Names and links of the user's playlists, so they can be played without internet."""
    import json

    try:
        PLAYLISTS_FILE.write_text(json.dumps([{"name": p["name"], "uri": p["uri"]} for p in items if p.get("uri")],
                                             ensure_ascii=False), encoding="utf-8")
    except (OSError, KeyError) as e:
        logger.debug("Couldn't save playlists: {}", e)


def saved_playlists() -> list[dict]:
    import json

    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def play_in_app(name: str, uri: str) -> str:
    """No internet (Spotify's web API unreachable): open the playlist in the Spotify app by its link and press its
    Play button through Windows accessibility - all on this PC, no cloud. Needs the songs downloaded to play."""
    import os

    from jarvis.agent.uia import run as on_uia
    from jarvis.tools.registry import wait_for
    from jarvis.tools.window_tools import find_window

    os.startfile(uri)
    win = wait_for(lambda: find_window("spotify"), timeout=12)
    if not win:
        return "ERROR: the Spotify app didn't open."

    def press_play():
        from pywinauto import Desktop

        from jarvis.tools.ui_tools import elements

        app = Desktop(backend="uia").window(handle=win["hwnd"])
        target = name.lower()
        for _ in range(16):  # the playlist page takes a moment to load
            for el in elements(app):
                label = (el.element_info.name or "").lower()
                if el.element_info.control_type == "Button" and label.startswith("play") and target in label:
                    try:
                        el.invoke()
                    except Exception:
                        el.click_input()
                    return True
            time.sleep(0.5)
        return False

    if not on_uia(press_play):
        return f"Opened your playlist '{name}' in Spotify, but couldn't find its Play button (no internet)."
    import win32gui

    title = wait_for(lambda: (t := win32gui.GetWindowText(win["hwnd"])) and not t.lower().startswith("spotify") and t,
                     timeout=6)
    if title:
        return f"Playing your playlist '{name}' (offline: started in the Spotify app; checked: now playing {title})."
    return (f"Pressed Play on '{name}' in the Spotify app, but nothing is playing yet - with no internet, only "
            "downloaded songs can play.")


def _describe(item: dict) -> str:
    artists = ", ".join(a["name"] for a in item.get("artists", [])[:2])
    return f"{item.get('name')}" + (f" by {artists}" if artists else "")


def register(reg: ToolRegistry, sp: Spotify) -> None:
    def run(fn):
        try:
            return fn()
        except SpotifyError as e:
            if str(e) == "NO_ACTIVE_DEVICE":
                return "ERROR: Spotify has no active device. Open the Spotify app and try again."
            return f"ERROR: {e}"

    @reg.tool("spotify_play", "Play on Spotify. kind: my_playlist (one of the user's own playlists, matched by "
              "name even if misheard), playlist, song, album, artist, or liked_songs.",
              {"query": {"type": "string", "description": "name of the song/playlist/album/artist"},
               "kind": {"type": "string",
                        "enum": ["my_playlist", "playlist", "song", "album", "artist", "liked_songs"]}},
              group="media")
    def spotify_play(query: str, kind: str):
        def go():
            if kind == "liked_songs":
                tracks = sp._call("GET", "/me/tracks", params={"limit": 50}).get("items", [])
                if not tracks:
                    return "Your Liked Songs is empty."
                sp.play(uris=[t["track"]["uri"] for t in tracks if t.get("track")])
                return "Playing your Liked Songs."
            if kind == "my_playlist":
                pls = sp.my_playlists()
                m = best_match(query, [p["name"] for p in pls])
                if m:
                    p = pls[m[0]]
                    sp.play(context_uri=p["uri"])
                    return f"Playing your playlist '{p['name']}'."
                names = ", ".join(p["name"] for p in pls[:15])
                return f"ERROR: none of your playlists matches '{query}'. Your playlists: {names}"
            api_kind = {"song": "track"}.get(kind, kind)
            items = sp.search(query, api_kind)
            if not items:
                return f"ERROR: nothing found on Spotify for '{query}'."
            top = items[0]
            if api_kind == "track":
                sp.play(uris=[top["uri"]])
            else:
                sp.play(context_uri=top["uri"])
            return f"Playing {kind} {_describe(top)}."

        try:
            result = run(go)
        except requests.RequestException:  # no internet: Spotify's web API is unreachable
            if kind == "my_playlist" and (pls := saved_playlists()):
                m = best_match(query, [p["name"] for p in pls])
                if m:
                    p = pls[m[0]]
                    return play_in_app(p["name"], p["uri"])
            return ("ERROR: no internet connection, so Spotify can't be controlled online. Offline I can only start "
                    "your own playlists (downloaded songs).")
        if result.startswith(("ERROR", "Your Liked Songs is empty")):
            return result
        playing = wait_for(lambda: (np := sp.now_playing()) and np.get("is_playing") and np.get("item"))
        if playing:
            return f"{result} Checked: now playing {_describe(playing)}."
        return (f"{result} WARNING: Spotify hasn't started playing yet (checked for 4 s). Tell the user it may "
                "need a moment, or that Spotify didn't respond.")

    @reg.tool("spotify_control", "Control Spotify playback: pause, resume, next, previous, shuffle_on, shuffle_off, "
              "repeat_on, repeat_off.",
              {"action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "shuffle_on",
                                                     "shuffle_off", "repeat_on", "repeat_off"]}}, group="media")
    def spotify_control(action: str):
        def go():
            if action == "resume":
                sp.ensure_device()
            path = {"pause": ("PUT", "/me/player/pause"), "resume": ("PUT", "/me/player/play"),
                    "next": ("POST", "/me/player/next"), "previous": ("POST", "/me/player/previous"),
                    "shuffle_on": ("PUT", "/me/player/shuffle?state=true"),
                    "shuffle_off": ("PUT", "/me/player/shuffle?state=false"),
                    "repeat_on": ("PUT", "/me/player/repeat?state=context"),
                    "repeat_off": ("PUT", "/me/player/repeat?state=off")}[action]
            sp._call(*path)
            return f"Spotify: {action.replace('_', ' ')}."

        return run(go)

    @reg.tool("spotify_volume", "Set Spotify's volume (0-100).", {"percent": {"type": "integer"}}, group="media")
    def spotify_volume(percent: int):
        pct = max(0, min(100, int(percent)))

        def go():
            sp._call("PUT", f"/me/player/volume?volume_percent={pct}")
            return f"Spotify volume {pct}%."

        return run(go)

    @reg.tool("spotify_now_playing", "What's playing on Spotify right now.", {}, group="media", read_only=True)
    def spotify_now_playing():
        def go():
            cur = sp.now_playing()
            if not cur or not cur.get("item"):
                return "Nothing is playing on Spotify."
            state = "Playing" if cur.get("is_playing") else "Paused"
            return f"{state}: {_describe(cur['item'])}."

        return run(go)

    @reg.tool("spotify_queue", "Add a song to the Spotify queue.", {"query": {"type": "string"}}, group="media")
    def spotify_queue(query: str):
        def go():
            items = sp.search(query, "track", 1)
            if not items:
                return f"ERROR: no song found for '{query}'."
            sp._call("POST", "/me/player/queue", params={"uri": items[0]["uri"]})
            return f"Queued {_describe(items[0])}."

        return run(go)

    @reg.tool("spotify_like_current", "Save the song that's playing to the user's Liked Songs.", {}, group="media")
    def spotify_like_current():
        def go():
            cur = sp.now_playing()
            if not cur or not cur.get("item"):
                return "Nothing is playing."
            sp._call("PUT", "/me/tracks", params={"ids": cur["item"]["id"]})
            return f"Liked {_describe(cur['item'])}."

        return run(go)

    @reg.tool("spotify_my_playlists", "List the user's Spotify playlists.", {}, group="media", read_only=True)
    def spotify_my_playlists():
        return run(lambda: [p["name"] for p in sp.my_playlists()][:50])


def setup(reg: ToolRegistry) -> Spotify | None:
    """Register the Spotify tools if a client id is set (they report how to log in if not logged in yet)."""
    cid = get_secret("spotify_client_id")
    if not cid:
        return None
    sp = Spotify(cid)
    register(reg, sp)
    return sp


def login_cli() -> None:
    cid = get_secret("spotify_client_id")
    if not cid:
        print("No Spotify client id. First run: .\\jarvis.ps1 set-key spotify_client_id")
        raise SystemExit(1)
    sp = Spotify(cid)
    print("A browser window opens: log in to Spotify and press Agree.")
    sp.login()
    n = len(sp.my_playlists())
    print(f"Connected to Spotify. Found {n} playlists.")
    logger.info("Spotify connected")


if __name__ == "__main__":
    login_cli()
