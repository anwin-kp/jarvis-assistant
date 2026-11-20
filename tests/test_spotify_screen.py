from types import SimpleNamespace

from jarvis.integrations import spotify as spm
from jarvis.integrations.spotify import Spotify, best_match
from jarvis.tools import ui_tools
from jarvis.tools.registry import ToolRegistry

OK = lambda t, a: True


def test_best_match_handles_misheard_playlist_names():
    names = ["Chill Vibes", "Mix-Up Mash", "Workout 2026", "Malayalam Hits"]
    for spoken in ["mix up mash", "Mixup Match", "mix-up mash", "the mix up mash playlist"]:
        assert names[best_match(spoken, names)[0]] == "Mix-Up Mash", spoken
    assert best_match("jazz classics", names) is None


class FakeSpotify(Spotify):
    def __init__(self, playlists):
        self._playlists, self._playlists_at, self.calls = playlists, 1e18, []
        self._token = {"refresh_token": "x"}

    def ensure_device(self, wait=15):
        return "dev1"

    def _call(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "/search":
            return {"tracks": {"items": [{"name": "Believer", "uri": "spotify:track:1",
                                          "artists": [{"name": "Imagine Dragons"}]}]}}
        if path == "/me/player/currently-playing":  # playback started: the tool checks this before saying so
            return {"is_playing": True, "item": {"name": "Track One", "artists": [{"name": "Artist"}]}}
        return {}


def test_spotify_play_my_playlist_and_song():
    sp = FakeSpotify([{"name": "Chill Vibes", "uri": "spotify:playlist:A"},
                      {"name": "Mix-Up Mash", "uri": "spotify:playlist:B"}])
    reg = ToolRegistry()
    spm.register(reg, sp)
    r = reg.execute("spotify_play", {"query": "mixup match", "kind": "my_playlist"}, OK)
    assert r.text == "Playing your playlist 'Mix-Up Mash'. Checked: now playing Track One by Artist."
    assert ("PUT", "/me/player/play?device_id=dev1", {"json": {"context_uri": "spotify:playlist:B"}}) in sp.calls
    r = reg.execute("spotify_play", {"query": "believer", "kind": "song"}, OK)
    assert "Believer by Imagine Dragons" in r.text
    r = reg.execute("spotify_play", {"query": "opera", "kind": "my_playlist"}, OK)
    assert r.text.startswith("ERROR") and "Chill Vibes" in r.text


def _el(ctype, name):
    return SimpleNamespace(element_info=SimpleNamespace(control_type=ctype, name=name))


def test_ui_find_and_describe():
    els = [_el("Button", "Play"), _el("ListItem", "Mix-Up Mash"), _el("Button", "Play next")]
    assert ui_tools.describe(els)[1] == "[1] ListItem: Mix-Up Mash"
    assert ui_tools.find(els, "play") is els[0]  # exact name wins over "Play next"
    assert ui_tools.find(els, "mix-up") is els[1]
    assert ui_tools.find(els, "2") is els[2]
    assert ui_tools.find(els, "pause") is None
