import numpy as np
import pytest
import yaml

from jarvis.tasks import playbooks as pb
from jarvis.tasks.playbooks import CURATED, Playbooks, load_cards, validate


def fake_embed(texts):
    """Bag-of-words vectors: similar wording -> similar vectors (enough to test the logic)."""
    vocab = {}
    vecs = []
    for t in texts:
        words = t.lower().replace("search_document:", "").replace("search_query:", "").split()
        v = np.zeros(512, np.float32)
        for w in words:
            v[hash(w.strip(".,?!")) % 512] += 1
        vecs.append(v)
    del vocab
    return np.stack(vecs)


@pytest.fixture
def tmp_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "LEARNED", tmp_path / "learned")
    monkeypatch.setattr(pb, "NOTES", tmp_path / "notes")
    return tmp_path


def real_tool_names():
    import tempfile
    from pathlib import Path

    import jarvis.app as app

    app.DATA = Path(tempfile.mkdtemp())
    _, _, _, reg, _, _ = app.build(dry_run=True)
    return reg, set(reg.names())


def test_every_curated_card_is_valid_and_uses_real_tools():
    reg, names = real_tool_names()
    files = sorted(CURATED.glob("*.yaml"))
    assert len(files) >= 20
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert validate(data, names) == [], f.name
        danger = [t for t in data["tools"] if reg.get(t).risk.value == "danger"]
        if danger:  # a playbook can't talk the AI out of an approval
            assert "DANGER" in data["safety"] and "approval" in data["safety"], f.name
    assert len(load_cards(CURATED, names)) == len(files)


def test_validation_catches_bad_cards():
    good = {"id": "x", "title": "t", "triggers": ["a"], "what": "w", "steps": ["open_app(name=x)"],
            "tools": ["open_app"], "verify": ["v"], "errors": [{"symptom": "s", "recovery": "r"}], "safety": "s"}
    assert validate(good, {"open_app"}) == []
    assert "unknown tool 'open_app'" in validate(good, set())
    assert validate({**good, "tools": ["open_app", "close_app"]}, {"open_app", "close_app"})  # unused tool
    assert validate({**good, "tools": ["delete_file"], "steps": ["delete_file(path=x)"]}, {"delete_file"},
                    learned=True)  # learned cards can't use dangerous tools


def ok(name, **args):
    return (name, args, "ok", "done (checked)")


def test_clean_success_becomes_candidate_then_verified(tmp_stores):
    books = Playbooks(fake_embed, {"spotify_play", "set_volume", "find_task_playbook"})
    books.cards = []  # no curated cards: test learning alone
    books.observe("play my sleep playlist and set volume to 20",
                  [ok("spotify_play", kind="my_playlist", query="sleep"), ok("set_volume", percent=20)])
    books.next_turn("thanks")
    learned = list((tmp_stores / "learned").glob("*.yaml"))
    assert len(learned) == 1
    data = yaml.safe_load(learned[0].read_text(encoding="utf-8"))
    assert data["status"] == "candidate" and data["tools"] == ["spotify_play", "set_volume"]
    assert books.for_prompt("play my sleep playlist and set volume to 20") is None  # candidates aren't injected
    books.observe("play my sleep playlist and set the volume to 20",
                  [ok("spotify_play", kind="my_playlist", query="sleep"), ok("set_volume", percent=20)])
    books.next_turn("great")
    data = yaml.safe_load(learned[0].read_text(encoding="utf-8"))
    assert data["status"] == "verified" and data["successes"] == 2


def test_failures_warnings_dangerous_and_corrected_runs_are_not_learned(tmp_stores):
    books = Playbooks(fake_embed, {"spotify_play", "set_volume", "delete_file", "find_task_playbook"})
    books.cards = []
    runs = [
        [ok("spotify_play"), ("set_volume", {}, "error", "boom")],
        [ok("spotify_play"), ("set_volume", {}, "ok", "WARNING: asked for 20% but the volume is 50%")],
        [ok("spotify_play"), ok("delete_file")],
        [ok("spotify_play")],  # single step: nothing to learn
    ]
    for trace in runs:
        books.observe("do the thing", trace)
        books.next_turn("ok")
    books.observe("play and set volume", [ok("spotify_play"), ok("set_volume")])
    books.next_turn("no, I meant the other playlist")  # corrected: rejected
    assert not (tmp_stores / "learned").exists() or not list((tmp_stores / "learned").glob("*.yaml"))


def test_a_correction_after_a_playbook_becomes_a_note(tmp_stores):
    books = Playbooks(fake_embed, {"open_app", "find_task_playbook"})
    books.last_used = "open-app"
    books.cards = [pb.Card({"id": "open-app", "title": "Open an application", "triggers": ["open notepad"],
                            "what": "w", "steps": ["open_app(name=x)"], "tools": ["open_app"], "verify": ["v"],
                            "errors": [], "safety": "safe"}, tmp_stores / "x.yaml")]
    books.next_turn("No, I meant Notepad++ not Notepad")
    assert "Notepad++" in books.cards[0].render()
    assert "Notepad++" in (tmp_stores / "notes" / "open-app.yaml").read_text(encoding="utf-8")
