from jarvis.tools import location_tools
from jarvis.tools.location_tools import Locator


def test_home_setting_wins(monkeypatch):
    monkeypatch.setattr(location_tools, "geocode", lambda q: {"place": "Panaji, Goa, India", "latitude": 15.5,
                                                              "longitude": 73.8})
    loc = Locator("Panaji, Goa").get()
    assert loc["source"] == "your home setting" and loc["latitude"] == 15.5


def test_windows_then_ip_and_failures_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(Locator, "_windows", staticmethod(lambda: calls.append("win")))
    monkeypatch.setattr(Locator, "_ip", staticmethod(lambda: calls.append("ip")))
    lo = Locator()
    assert lo.get() is None and lo.get() is None and calls == ["win", "ip"]  # second call: cached failure


def test_windows_fix_gets_a_place_name(monkeypatch):
    monkeypatch.setattr(Locator, "_windows", staticmethod(
        lambda: {"latitude": 15.5, "longitude": 73.8, "accuracy_m": 20000, "source": "network (IP)"}))
    monkeypatch.setattr(Locator, "_name", staticmethod(lambda lat, lon: {"city": "Panaji",
                                                                          "place": "Panaji, Goa, India"}))
    lo = Locator()
    assert lo.city() == "Panaji, Goa, India" and lo.get()["source"] == "network (IP)"
