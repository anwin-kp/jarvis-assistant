"""Where the user is, so "weather", "near me" and "what time is it there" work without naming a place.

Order: config location.home (exact, if set) -> Windows location service (Wi-Fi/GPS when available, else the
network's IP; respects Settings > Privacy > Location) -> IP lookup as a last resort. Cached for 30 minutes."""

import asyncio
import threading
import time

import requests
from loguru import logger

from jarvis.tools.registry import ToolRegistry
from jarvis.tools.web_tools import UA, geocode

CACHE_S = 30 * 60
SOURCES = {0: "cellular", 1: "GPS", 2: "Wi-Fi", 3: "network (IP)", 4: "unknown", 5: "default", 6: "obfuscated"}


class Locator:
    def __init__(self, home: str | None = None):
        self.home = (home or "").strip()
        self._cached: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def get(self, refresh: bool = False) -> dict | None:
        """{"latitude", "longitude", "place", "city", "accuracy_m", "source"} or None."""
        with self._lock:
            if self._cached and not refresh and time.monotonic() - self._cached[0] < CACHE_S:
                return self._cached[1]
            loc = self._locate()
            # a failure is remembered for 5 minutes, so offline turns don't each wait on network timeouts
            self._cached = (time.monotonic() - (0 if loc else CACHE_S - 300), loc)
            return loc

    def city(self) -> str | None:
        try:
            loc = self.get()
        except Exception:
            return None
        return loc and loc.get("place")

    def _locate(self) -> dict | None:
        if self.home:
            g = geocode(self.home)
            if g:
                return {**g, "city": self.home, "accuracy_m": 100, "source": "your home setting"}
        for find in (self._windows, self._ip):
            try:
                loc = find()
            except Exception as e:
                logger.debug("Location via {} failed: {}", find.__name__, e)
                loc = None
            if loc:
                return {**loc, **self._name(loc["latitude"], loc["longitude"])}
        return None

    @staticmethod
    def _windows() -> dict | None:
        from winrt.windows.devices.geolocation import GeolocationAccessStatus, Geolocator, PositionAccuracy

        from jarvis.agent.operator import run_isolated

        async def fix():
            if await Geolocator.request_access_async() != GeolocationAccessStatus.ALLOWED:
                logger.info("Windows location is off for apps (Settings > Privacy & security > Location)")
                return None
            g = Geolocator()
            g.desired_accuracy = PositionAccuracy.HIGH
            pos = await asyncio.wait_for(g.get_geoposition_async(), 8)
            c = pos.coordinate
            return {"latitude": c.point.position.latitude, "longitude": c.point.position.longitude,
                    "accuracy_m": round(c.accuracy), "source": SOURCES.get(int(c.position_source), "unknown")}

        return run_isolated(lambda: asyncio.run(fix()))  # WinRT in its own thread (COM threading mode)

    @staticmethod
    def _ip() -> dict | None:
        j = requests.get("https://ipapi.co/json/", headers=UA, timeout=6).json()
        if j.get("latitude") is None:
            return None
        return {"latitude": j["latitude"], "longitude": j["longitude"], "accuracy_m": 20000, "source": "IP lookup"}

    @staticmethod
    def _name(lat: float, lon: float) -> dict:
        """Coordinates -> "Panaji, Goa, India" (OpenStreetMap reverse geocoding)."""
        try:
            a = requests.get("https://nominatim.openstreetmap.org/reverse", headers=UA, timeout=6,
                             params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
                                     "accept-language": "en"}).json().get("address", {})
        except (requests.RequestException, ValueError):
            a = {}
        city = a.get("city") or a.get("town") or a.get("village") or a.get("county") or a.get("state_district")
        place = ", ".join(dict.fromkeys(filter(None, (city, a.get("state"), a.get("country")))))
        return {"city": city or "", "place": place or f"{lat:.3f}, {lon:.3f}"}


def register(reg: ToolRegistry, cfg: dict) -> Locator:
    locator = Locator((cfg.get("location") or {}).get("home"))
    reg.locator = locator

    @reg.tool("get_my_location", "Where the user is right now (city, region, country, coordinates). Use for "
              "'where am I', and before answering anything 'near me' / 'around here' / 'local'.",
              {}, group="web", read_only=True, remember_last="disk")
    def get_my_location():
        loc = locator.get()
        if not loc:
            return ("ERROR: couldn't get your location (Windows location is off and the network lookup failed). "
                    "Set location.home in config.yaml to fix it.")
        return {k: loc[k] for k in ("place", "latitude", "longitude", "accuracy_m", "source")}

    return locator
