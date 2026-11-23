"""Voice ID on onnxruntime (sherpa-onnx is blocked by Smart App Control): features, shadow mode, the report."""

import json
import sqlite3
import types

import numpy as np

from jarvis.audio import voiceid


def test_fbank_has_the_shape_and_scale_the_model_expects():
    t = np.arange(16000) / 16000
    feats = voiceid.fbank((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
    assert feats.shape == (98, 80) and np.isfinite(feats).all()
    assert feats[:, :].argmax(axis=1).std() < 1  # a pure tone lands in the same mel band in every frame
    assert voiceid.fbank(np.zeros(100, np.float32)).shape == (1, 80)  # shorter than a frame: padded, no crash


def test_report_needs_data_and_only_suggests_a_threshold_when_voices_separate(tmp_path):
    db = tmp_path / "j.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT, text TEXT)")
    log = tmp_path / "log"
    log.mkdir()

    def add(n, text, score, answered):
        (log / f"{n}.json").write_text(json.dumps({"text": text, "voice_score": score}))
        if answered:
            con.execute("INSERT INTO messages(role, text) VALUES ('user', ?)", (text,))

    for i in range(3):
        add(i, f"hey jarvis {i}", 0.7, True)
    con.commit()
    assert voiceid.report(log, db).startswith("Not enough data")
    for i in range(3, 15):
        add(i, f"hey jarvis {i}", 0.65 + i / 100, True)
    for i in range(15, 30):
        add(i, f"tv line {i}", 0.2 + (i % 5) / 100, False)
    con.commit()
    out = voiceid.report(log, db)
    assert "They separate" in out and "threshold" in out
    for i in range(30, 50):
        add(i, f"tv shouting {i}", 0.75, False)  # the TV sounds just like you: overlap
    con.commit()
    assert "overlap" in voiceid.report(log, db)


def test_shadow_mode_measures_but_never_blocks():
    from jarvis.__main__ import Voice

    v = Voice.__new__(Voice)
    v.voiceid = types.SimpleNamespace(is_owner=lambda audio: (False, 0.31))
    v.enforce_voice_id = False
    assert v.is_owner(np.zeros(32000)) is True and v.voice_score == 0.31  # measured, not blocked
    v.enforce_voice_id = True
    assert v.is_owner(np.zeros(32000)) is False  # voice_id.enabled: true enforces it
