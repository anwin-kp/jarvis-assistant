"""Offline: internet tools answer with their last good result (and its age); real errors are never hidden."""
from jarvis.tools import last_known
from jarvis.tools.last_known import LastKnown
from jarvis.tools.registry import ToolRegistry


def make(tmp_path, where="disk"):
    reg = ToolRegistry()
    reg.last_known = LastKnown(tmp_path / "lk.json")
    state = {"fail": False}

    @reg.tool("get_weather", "w", {"city": {"type": "string"}}, required=[], read_only=True, remember_last=where)
    def get_weather(city: str = ""):
        if state["fail"]:
            raise ConnectionError("no route to host")
        return f"Sunny in {city or 'Kochi'}"

    return reg, state


def test_offline_answers_with_last_known_and_its_age(tmp_path, monkeypatch):
    reg, state = make(tmp_path)
    assert reg.execute("get_weather", {}, None).text == "Sunny in Kochi"
    state["fail"] = True
    monkeypatch.setattr(last_known, "internet_up", lambda timeout=1.5: False)
    r = reg.execute("get_weather", {}, None)
    assert not r.error and r.text.startswith("OFFLINE") and "just now" in r.text and "Sunny in Kochi" in r.text
    # it survives a restart (weather is saved to disk)
    reg2, state2 = make(tmp_path)
    state2["fail"] = True
    assert "Sunny in Kochi" in reg2.execute("get_weather", {}, None).text


def test_real_errors_are_not_covered_up(tmp_path, monkeypatch):
    reg, state = make(tmp_path)
    reg.execute("get_weather", {}, None)
    state["fail"] = True
    monkeypatch.setattr(last_known, "internet_up", lambda timeout=1.5: True)  # online: a genuine failure
    r = reg.execute("get_weather", {}, None)
    assert r.error and "OFFLINE" not in r.text


def test_no_earlier_result_says_so(tmp_path, monkeypatch):
    reg, state = make(tmp_path)
    state["fail"] = True
    monkeypatch.setattr(last_known, "internet_up", lambda timeout=1.5: False)
    assert "no internet connection" in reg.execute("get_weather", {"city": "Paris"}, None).text


def test_email_results_never_touch_disk(tmp_path):
    reg, _ = make(tmp_path, where="memory")
    reg.execute("get_weather", {}, None)
    assert not (tmp_path / "lk.json").exists()


def test_today_is_keyed_by_the_real_date(monkeypatch):
    import datetime as dt

    k1 = last_known._key("list_events", {"day": "today"})

    class Tomorrow(dt.date):
        @classmethod
        def today(cls):
            return dt.date.today() + dt.timedelta(days=1)

    monkeypatch.setattr(last_known, "date", Tomorrow)
    assert last_known._key("list_events", {"day": "today"}) != k1


def test_same_question_same_key():
    k = last_known._key
    assert k("get_weather", {}) == k("get_weather", {"city": ""}) == k("get_weather", {"city": "Here"})
    assert k("get_weather", {"city": "Paris"}) != k("get_weather", {})
