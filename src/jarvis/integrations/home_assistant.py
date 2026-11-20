"""Optional smart-home control via Home Assistant's REST API (free, self-hosted).
Config: home_assistant.url; token: .\\jarvis.ps1 set-key home_assistant"""

import requests

from jarvis.core.config import get_secret
from jarvis.tools.registry import Risk, ToolRegistry

SENSITIVE_DOMAINS = {"lock", "alarm_control_panel", "cover", "garage_door"}  # doors, alarms: always DANGER


class HomeAssistant:
    def __init__(self, url: str, token: str):
        self.url, self.headers = url.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def get(self, path: str):
        r = requests.get(f"{self.url}/api/{path}", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def call(self, domain: str, service: str, entity_id: str):
        r = requests.post(f"{self.url}/api/services/{domain}/{service}", headers=self.headers,
                          json={"entity_id": entity_id}, timeout=10)
        r.raise_for_status()
        return r.json()


def register(reg: ToolRegistry, cfg: dict) -> bool:
    token = get_secret("home_assistant_api_key")
    if not cfg.get("url") or not token:
        return False
    ha = HomeAssistant(cfg["url"], token)
    allowed = set(cfg.get("domains", ["light", "switch", "fan", "media_player", "climate", "scene"]))

    @reg.tool("list_devices", "List smart-home devices (entity id, name, state).",
              {"domain": {"type": "string", "description": "e.g. light, switch; empty for all"}}, required=[],
              group="home", read_only=True)
    def list_devices(domain: str = ""):
        rows = [s for s in ha.get("states") if not domain or s["entity_id"].startswith(domain + ".")]
        return [{"entity_id": s["entity_id"], "name": s["attributes"].get("friendly_name"), "state": s["state"]}
                for s in rows[:60]]

    @reg.tool("control_device", "Control a smart-home device, e.g. domain=light service=turn_on "
              "entity_id=light.bedroom.",
              {"domain": {"type": "string"}, "service": {"type": "string"}, "entity_id": {"type": "string"}},
              risk=Risk.CONFIRM, group="home")
    def control_device(domain: str, service: str, entity_id: str):
        if domain in SENSITIVE_DOMAINS or domain not in allowed:
            raise PermissionError(f"Domain '{domain}' isn't allowed (config home_assistant.domains)")
        if not entity_id.startswith(domain + "."):
            raise PermissionError("entity_id must belong to the same domain")
        ha.call(domain, service, entity_id)
        return f"Done: {service} on {entity_id}."

    return True
