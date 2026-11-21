"""Example JARVIS plugin: dice, coin flips and world clocks.
Copy this file as a template for your own tools. Restart JARVIS to load changes."""

import random
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

KEYWORDS = ["dice", "roll", "coin", "flip", "time in", "time zone", "timezone", "o'clock in"]


def register(reg, ctx):
    @reg.tool("roll_dice", "Roll dice, e.g. sides=6 count=2.",
              {"sides": {"type": "integer"}, "count": {"type": "integer"}}, required=[])
    def roll_dice(sides: int = 6, count: int = 1):
        rolls = [random.randint(1, max(2, int(sides))) for _ in range(max(1, min(int(count), 20)))]
        return {"rolls": rolls, "total": sum(rolls)}

    @reg.tool("flip_coin", "Flip a coin.", {})
    def flip_coin():
        return random.choice(["Heads", "Tails"])

    @reg.tool("world_time", "Current time in a city or IANA time zone, e.g. 'London' or 'America/New_York'.",
              {"place": {"type": "string"}}, read_only=True)
    def world_time(place: str):
        p = place.strip().replace(" ", "_").lower()
        zone = next((z for z in available_timezones() if z.lower() == p or z.lower().endswith("/" + p)), None)
        if not zone:
            return f"ERROR: unknown time zone or city '{place}'. Try a major city or e.g. Europe/Paris."
        return f"{datetime.now(ZoneInfo(zone)):%A %H:%M} in {zone}"
