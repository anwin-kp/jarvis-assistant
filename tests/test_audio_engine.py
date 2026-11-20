"""Deterministic tests for the AudioEngine: frames are fed directly, speech = loud frames."""

import numpy as np

from jarvis.audio.engine import FRAME, AudioEngine

QUIET, VOICE, ECHO, LOUD = 0.001, 0.05, 0.03, 0.2


def frames(level: float, ms: int) -> list[np.ndarray]:
    """Frames of noise with the given RMS level (deterministic)."""
    rng = np.random.default_rng(int(level * 1e6) + ms)
    return [(rng.standard_normal(FRAME) * level * 32768).clip(-32768, 32767).astype(np.int16)
            for _ in range(ms // 30)]


def make(**kw):
    events = []
    eng = AudioEngine(lambda k, d: events.append((k, d)), is_speech=lambda f: True, calibrate_ms=300, **kw)
    feed(eng, frames(QUIET, 300))  # calibration: quiet room
    return eng, events


def feed(eng, fs):
    for f in fs:
        eng.process(f)


def kinds(events):
    return [k for k, _ in events if k != "level"]


def utterances(events):
    return [d for k, d in events if k == "utterance"]


def test_calibration_sets_threshold_above_room_noise():
    eng, _ = make()
    assert eng.min_threshold <= eng.threshold < VOICE


def test_always_mode_captures_one_complete_utterance():
    eng, ev = make()
    feed(eng, frames(VOICE, 1200) + frames(QUIET, 900))
    assert kinds(ev) == ["speech_start", "utterance"]
    u = utterances(ev)[0]
    assert 1.2 <= u.audio.size / 16000 <= 2.3 and not u.barge


def test_pause_shorter_than_silence_window_does_not_split_sentence():
    eng, ev = make(silence_ms=750)
    feed(eng, frames(VOICE, 600) + frames(QUIET, 450) + frames(VOICE, 600) + frames(QUIET, 900))
    assert len(utterances(ev)) == 1


def test_short_noise_is_not_an_utterance():
    eng, ev = make(min_speech_ms=400)
    feed(eng, frames(VOICE, 120) + frames(QUIET, 900))
    assert "utterance" not in kinds(ev) and "noise" in kinds(ev)


def test_echo_while_speaking_is_ignored():
    eng, ev = make()
    eng.set_speaking(True)
    feed(eng, frames(ECHO, 3000))  # JARVIS's own voice coming back through the mic
    eng.set_speaking(False)
    feed(eng, frames(ECHO, 300) + frames(QUIET, 1200))  # echo tail right after speaking
    assert "barge" not in kinds(ev) and not utterances(ev)


def test_barge_in_captures_your_whole_sentence():
    eng, ev = make()
    eng.set_speaking(True)
    feed(eng, frames(ECHO, 900))  # JARVIS talking
    feed(eng, frames(LOUD, 1200))  # you talk over it
    feed(eng, frames(QUIET, 900))
    assert kinds(ev) == ["barge", "speech_start", "utterance"]
    u = utterances(ev)[0]
    assert u.barge and u.audio.size / 16000 >= 1.1  # the start of your sentence is kept


def test_jarvis_own_voice_never_triggers_barge_in():
    events = []
    eng = AudioEngine(lambda k, d: events.append((k, d)), is_speech=lambda f: True, calibrate_ms=300,
                      sounds_like_jarvis=lambda audio: True)
    feed(eng, frames(QUIET, 300))
    eng.set_speaking(True)
    # borderline: louder than the learned echo (0.03 -> threshold 0.054) but not overwhelmingly (< 0.12),
    # so the voice fingerprint decides - and it recognises JARVIS
    feed(eng, frames(ECHO, 600) + frames(0.07, 1500))
    assert "barge" not in kinds(events)


def test_push_mode_only_captures_when_requested():
    eng, ev = make()
    eng.set_mode("push")
    feed(eng, frames(VOICE, 900) + frames(QUIET, 900))
    assert not utterances(ev)
    eng.request_capture(timeout_s=3)
    feed(eng, frames(VOICE, 900) + frames(QUIET, 900))
    assert len(utterances(ev)) == 1
    eng.request_capture(timeout_s=0.3)
    feed(eng, frames(QUIET, 600))
    assert "capture_timeout" in kinds(ev)


def test_wake_mode_wakes_then_captures_follow_up():
    calls = {"n": 0}

    def detector(frame):
        calls["n"] += 1
        return calls["n"] == 5

    events = []
    eng = AudioEngine(lambda k, d: events.append((k, d)), is_speech=lambda f: True, calibrate_ms=300,
                      wake_detector=detector)
    feed(eng, frames(QUIET, 300))
    eng.set_mode("wake")
    feed(eng, frames(QUIET, 300) + frames(VOICE, 900) + frames(QUIET, 900))
    assert kinds(events)[:2] == ["wake", "speech_start"]
    assert utterances(events)[0].after_wake


def test_jarvis_speaking_ends_a_capture_without_swallowing_its_voice():
    eng, ev = make()
    feed(eng, frames(VOICE, 900))  # you are talking...
    eng.set_speaking(True)  # ...and JARVIS starts speaking
    feed(eng, frames(ECHO, 1500))
    u = utterances(ev)
    assert len(u) == 1 and u[0].audio.size / 16000 <= 1.3  # only your part, none of JARVIS's speech


def test_loud_headset_speech_interrupts_even_if_fingerprint_is_unsure():
    events = []
    eng = AudioEngine(lambda k, d: events.append((k, d)), is_speech=lambda f: True, calibrate_ms=300,
                      sounds_like_jarvis=lambda audio: True)  # fingerprint (wrongly) says "JARVIS"
    feed(eng, frames(QUIET, 300))
    eng.set_speaking(True)
    feed(eng, frames(QUIET * 3, 600) + frames(LOUD, 900) + frames(QUIET, 900))  # low echo, you speak clearly
    assert "barge" in kinds(events)


def test_calibration_ignores_jarvis_talking_and_follows_the_room():
    from jarvis.audio.engine import rms

    events = []
    # like the real voice detector: a fan's steady hum is not speech, a voice is
    eng = AudioEngine(lambda k, d: events.append((k, d)), is_speech=lambda f: rms(f) > 0.02, calibrate_ms=300)
    eng.set_speaking(True)
    feed(eng, frames(0.05, 900))  # JARVIS's startup greeting: must not count as room noise
    eng.set_speaking(False)
    feed(eng, frames(QUIET, 700 + 300))  # echo tail, then a quiet room
    assert eng.threshold < 0.01
    feed(eng, frames(0.006, 3000))  # a fan switches on: background rises
    assert 0.012 <= eng.threshold <= 0.03


def test_short_stop_word_interrupts():
    eng, ev = make()
    eng.set_speaking(True)
    feed(eng, frames(QUIET, 600))
    # "stop": ~360 ms of speech with a short dip in the middle (not unbroken)
    feed(eng, frames(LOUD, 150) + frames(QUIET, 60) + frames(LOUD, 150) + frames(QUIET, 900))
    assert "barge" in kinds(ev)


def test_speech_across_a_sentence_boundary_still_interrupts():
    eng, ev = make()
    eng.set_speaking(True)
    feed(eng, frames(QUIET, 900) + frames(LOUD, 90))
    eng.set_speaking(False)  # JARVIS finishes a sentence...
    eng.set_speaking(True)  # ...and starts the next one: same reply, no reset
    feed(eng, frames(LOUD, 120) + frames(QUIET, 900))
    assert "barge" in kinds(ev)


def test_one_word_command_is_kept_and_reports_speech_length():
    eng, ev = make()
    feed(eng, frames(VOICE, 300) + frames(QUIET, 900))  # "stop": ~300 ms of speech
    u = utterances(ev)
    assert len(u) == 1 and 250 <= u[0].voiced_ms <= 400


def test_very_short_interrupt_is_reported_as_a_stop():
    eng, ev = make()
    eng.set_speaking(True)
    feed(eng, frames(QUIET, 600) + frames(LOUD, 180) + frames(QUIET, 900))  # a clipped "stop"
    assert ("noise", True) in [(k, d) for k, d in ev if k == "noise"] and "barge" in kinds(ev)
