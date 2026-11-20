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
