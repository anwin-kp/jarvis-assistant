from datetime import date, timedelta

from jarvis.integrations.google import _body_text, _day_range


def test_day_range():
    b, e = _day_range("tomorrow")
    assert b.date() == date.today() + timedelta(days=1) and (e - b).days == 1
    assert _day_range("2026-12-25")[0].date() == date(2026, 12, 25)


def test_body_text_finds_plain_part():
    import base64

    data = base64.urlsafe_b64encode(b"hello there").decode()
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": "x"}},
        {"mimeType": "text/plain", "body": {"data": data}},
    ]}
    assert _body_text(payload) == "hello there"
