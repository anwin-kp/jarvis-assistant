import queue

from fastapi.testclient import TestClient

from jarvis.ui.hud import Hud


def test_hud_requires_token_and_relays_messages():
    q = queue.Queue()
    hud = Hud(q)
    client = TestClient(hud._make_app())
    assert client.get("/").status_code == 403
    assert client.get("/?token=wrong").status_code == 403
    assert "JARVIS" in client.get(f"/?token={hud.token}").text
    hud.publish("reply", text="hello")  # goes to history, replayed on connect
    with client.websocket_connect(f"/ws?token={hud.token}") as ws:
        assert ws.receive_json() == {"type": "reply", "text": "hello"}
        ws.send_json({"action": "text", "text": "what time is it"})
        ws.send_json({"action": "talk"})
    assert q.get(timeout=2) == ("text", "what time is it")
    assert q.get(timeout=2) == ("text", "")


def test_gauges_are_measured_only_while_a_window_shows_them():
    import queue
    import types

    from jarvis.ui.hud import Hud

    h = Hud(queue.Queue())
    assert not h.shows_stats  # no window open
    overlay, window = types.SimpleNamespace(), types.SimpleNamespace()
    h.clients |= {overlay}
    h.no_stats.add(overlay)
    assert not h.shows_stats  # the overlay hides the gauges
    h.clients.add(window)
    assert h.shows_stats
