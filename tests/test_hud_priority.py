import queue
import time

from fastapi.testclient import TestClient

from jarvis.ui.hud import Hud


def hud_with(on_stop=lambda t: False):
    q, order = queue.Queue(), []
    h = Hud(q)
    h.on_stop = on_stop
    h.on_input = lambda text: order.append(("cut-in", text, q.qsize()))
    return h, q, order


def send(h, *msgs):
    with TestClient(h._make_app()).websocket_connect(f"/ws?token={h.token}") as ws:
        for m in msgs:
            ws.send_json(m)
        time.sleep(0.3)


def test_buttons_and_messages_cut_in_before_they_are_queued():
    h, q, order = hud_with()
    send(h, {"action": "command", "text": "/mute"}, {"action": "text", "text": "what time is it"})
    # each cut-in happened while nothing of its own was queued yet
    assert order == [("cut-in", "/mute", 0), ("cut-in", "what time is it", 1)]
    assert [q.get(), q.get()] == [("text", "/mute"), ("text", "what time is it")]


def test_typed_stop_is_a_stop_not_a_message():
    stops = []
    h, q, order = hud_with(on_stop=lambda t: stops.append(t) or t == "stop")
    send(h, {"action": "text", "text": "stop"})
    assert stops == ["stop"] and order == [] and q.empty()


def test_an_approval_answer_does_not_cut_in():
    from jarvis.__main__ import App

    app = App.__new__(App)
    calls = []
    app.interrupt = lambda text, force=False: calls.append(text)
    app._awaiting_confirm = True
    app._speech_q = queue.Queue()
    app._speech_q.put("a sentence being spoken")  # -> speaking
    app.busy = type("E", (), {"is_set": lambda self: True})()
    app.preempt("yes")
    assert calls == []
    app._awaiting_confirm = False
    app.preempt("/mute")
    assert calls == ["HUD: /mute"]


def test_hej_is_hey_and_jarvis_needs_its_name_after_a_quiet_minute():
    import time as _t
    from types import SimpleNamespace

    from jarvis.__main__ import App, normalize_heard

    assert normalize_heard("Hej Jarvis.") == "Hey Jarvis." and normalize_heard("hej") == "Hey"
    assert normalize_heard("Hejduk is a name") == "Hejduk is a name"
    app = App.__new__(App)
    app.cfg = {"voice": {"addressed_window_s": 60}}
    app._engine_mode = lambda: "always"
    app.fast = SimpleNamespace(matches=lambda t: t.lower() == "pause")
    utt = SimpleNamespace(after_wake=False, barge=False)
    app._spoke_at = app._last_heard = _t.monotonic() - 300  # quiet for 5 minutes
    assert not app._addressed("We'd sneak it in making protein shakes", utt)  # someone else talking
    assert app._addressed("Jarvis, what's the weather?", utt) and app._addressed("pause", utt)
    app._spoke_at = _t.monotonic() - 10  # JARVIS just replied: follow-ups need no name
    assert app._addressed("and tomorrow?", utt)


def test_measured_mishearings_are_corrected():
    from jarvis.__main__ import normalize_heard
    from jarvis.core.commands import is_stop_command

    assert normalize_heard("Bosta Music") == "Pause the music" and normalize_heard("Posta musical.") != "Posta musical."
    assert is_stop_command("Go.") and is_stop_command("stop") and not is_stop_command("go to sleep")


def test_short_words_are_kept_but_only_as_commands():
    import numpy as np

    from jarvis.__main__ import SHORT_WORDS
    from jarvis.audio.engine import AudioEngine

    for w in ("Stop.", "Go.", "Continue", "next", "Yes.", "pause", "Thanks, Jarvis."):
        assert SHORT_WORDS.match(w), w
    for w in ("Thank you for watching.", "Uh", "the", "Bye bye bye"):
        assert not SHORT_WORDS.match(w), w

    events = []
    eng = AudioEngine(lambda kind, data: events.append((kind, data)), min_speech_ms=250)
    frame = np.zeros(480, np.int16)
    for voiced, want in ((6, "short utterance"), (2, "noise"), (10, "utterance")):  # 180 ms / 60 ms / 300 ms
        events.clear()
        eng._frames, eng._voiced, eng._capture_barge, eng._capture_after_wake = [frame] * 20, voiced, False, False
        eng._finish_capture()
        kind, data = events[-1]
        got = "noise" if kind == "noise" else ("short utterance" if data.short else "utterance")
        assert got == want, (voiced, got)


def test_distant_speech_is_kept_but_silence_and_gibberish_are_not():
    from jarvis.stt.groq_stt import keep_segment

    assert keep_segment(0.00, -1.12)  # mic away from the mouth: real speech, lower confidence (was dropped)
    assert not keep_segment(0.85, -1.3)  # probably silence AND low confidence
    assert keep_segment(0.85, -0.4)  # confident words are kept
    assert not keep_segment(0.0, -2.4)  # gibberish


def test_own_voice_coming_back_is_recognised_as_echo():
    from jarvis.__main__ import echo_of

    said = ["You have no unread emails.", "The time in Tokyo is 1:38 AM."]
    assert echo_of("You have no", said) and echo_of("the time in time.", said)
    assert not echo_of("Stop.", said)  # one-word commands are never echo
    assert not echo_of("play my Mix-Up Mash playlist", said) and not echo_of("what's the weather like?", said)


def test_no_self_interrupt_from_late_loud_echo():
    import numpy as np

    from jarvis.audio.engine import AudioEngine

    events = []
    eng = AudioEngine(lambda k, d: events.append(k), is_speech=lambda f: True, calibrate_ms=300,
                      sounds_like_jarvis=lambda a: False)
    quiet = np.zeros(480, np.int16)
    loud_echo = (np.ones(480) * 0.25 * 32767).astype(np.int16)  # Bluetooth echo arriving 0.4 s late, very loud
    for _ in range(20):
        eng.process(quiet)
    eng.set_speaking(True)
    for f in [quiet] * 13 + [loud_echo] * 60:
        eng.process(f)
    assert "barge" not in events
