from types import SimpleNamespace

import jarvis.core.proactive as pro
from jarvis.core.config import list_personas, load_persona
from jarvis.integrations import home_assistant as ha
from jarvis.tools.registry import ToolRegistry


def test_proactive_alerts_with_cooldown(monkeypatch):
    monkeypatch.setattr(pro.psutil, "sensors_battery", lambda: SimpleNamespace(percent=9, power_plugged=False))
    monkeypatch.setattr(pro.psutil, "disk_usage", lambda p: SimpleNamespace(free=50 * 2**30))
    alerts = []
    p = pro.Proactive(alerts.append, cooldown_minutes=30)
    p.check(now=0)
    p.check(now=60)  # within cooldown: no repeat
    assert len(alerts) == 1 and "9 percent" in alerts[0]
    p.check(now=31 * 60)
    assert len(alerts) == 2


def test_personas():
    assert {"default", "butler", "casual", "terse"} <= set(list_personas())
    assert "butler" in load_persona("butler") and load_persona("default") == load_persona(None)


def test_home_assistant_blocks_locks(monkeypatch):
    monkeypatch.setattr(ha, "get_secret", lambda n: "tok")
    reg = ToolRegistry()
    assert ha.register(reg, {"url": "http://ha.local:8123"})
    r = reg.execute("control_device", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
                    lambda t, a: True)
    assert r.error and "allowed" in r.text
    r = reg.execute("control_device", {"domain": "light", "service": "turn_on", "entity_id": "lock.front"},
                    lambda t, a: True)
    assert r.error
    assert not ha.register(ToolRegistry(), {"url": None})
