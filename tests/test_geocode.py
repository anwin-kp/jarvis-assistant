from jarvis.tools import web_tools


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok

    def json(self):
        return self.data


def fake_get(nominatim, open_meteo):
    calls = []

    def get(url, params=None, **kw):
        calls.append((url, params))
        return Resp(nominatim if "nominatim" in url else {"results": open_meteo})

    return get, calls


def test_nominatim_first(monkeypatch):
    monkeypatch.setattr(web_tools, "_geo_cache", {})
    monkeypatch.setattr(web_tools, "_geo_last", [0.0])
    get, calls = fake_get([{"display_name": "Goa, India", "lat": "15.3", "lon": "74.09"}], [])
    monkeypatch.setattr(web_tools.requests, "get", get)
    loc = web_tools.geocode("Goa", "India")
    assert loc == {"place": "Goa, India", "latitude": 15.3, "longitude": 74.09}
    assert calls[0][1]["q"] == "Goa, India" and len(calls) == 1
    assert web_tools.geocode("goa", "india") == loc and len(calls) == 1  # cached


def test_falls_back_to_open_meteo_preferring_the_exact_name(monkeypatch):
    monkeypatch.setattr(web_tools, "_geo_cache", {})
    monkeypatch.setattr(web_tools, "_geo_last", [0.0])
    om = [{"name": "Genoa", "country": "Italy", "population": 580000, "latitude": 44.4, "longitude": 8.9},
          {"name": "Goa", "country": "Philippines", "admin1": "Bicol", "population": 60000, "latitude": 13.7,
           "longitude": 123.5}]
    get, _ = fake_get([], om)
    monkeypatch.setattr(web_tools.requests, "get", get)
    assert web_tools.geocode("Goa")["place"] == "Goa, Bicol, Philippines"


def test_forecast_falls_back_when_open_meteo_is_overloaded(monkeypatch):
    def get(url, params=None, **kw):
        if "open-meteo" in url:
            return Resp({"error": True, "reason": "The service is overloaded"})
        return Resp({"current_condition": [{"temp_C": "24", "FeelsLikeC": "25", "humidity": "60", "windspeedKmph": "9",
                                            "weatherDesc": [{"value": "Sunny "}]}],
                     "weather": [{"maxtempC": "33", "mintempC": "18",
                                  "hourly": [{"chanceofrain": "4", "weatherDesc": [{"value": "Clear"}]}]}]})

    monkeypatch.setattr(web_tools.requests, "get", get)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = web_tools.forecast(15.5, 73.8)
    assert r["now"]["temperature_c"] == 24 and r["now"]["conditions"] == "sunny" and r["today"]["max_c"] == 33
