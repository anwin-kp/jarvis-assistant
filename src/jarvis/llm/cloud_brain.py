"""Optional free-tier cloud brain (Groq / Google Gemini / OpenRouter) via their OpenAI-compatible APIs,
plus the router that decides local vs cloud. Off by default: JARVIS stays 100% local unless you add a key."""

import requests
from loguru import logger

from jarvis.core.config import get_secret
from jarvis.llm.ollama_brain import CLAIMS_ACTION, NUDGE
from jarvis.tools.registry import ConfirmFn, ToolRegistry

PROVIDERS = {  # base URL, keyring entry name
    "groq": ("https://api.groq.com/openai/v1", "groq_api_key"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini_api_key"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter_api_key"),
}
PRIVATE_GROUPS = {"files", "email"}  # never sent to the cloud


class CloudUnavailable(RuntimeError):
    pass


class CloudBrain:
    def __init__(self, registry: ToolRegistry, provider: str, model: str, max_steps: int = 6, timeout: float = 25):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}")
        self.base, key_name = PROVIDERS[provider]
        self.key = get_secret(key_name)
        self.reg, self.provider, self.model = registry, provider, model
        self.max_steps, self.timeout = max_steps, timeout
        self.last_tool_calls: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            body["tools"] = tools
        try:
            r = requests.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.key}"})
        except requests.RequestException as e:
            raise CloudUnavailable(f"network: {e}") from e
        if r.status_code == 429 or r.status_code >= 500:
            raise CloudUnavailable(f"{self.provider} HTTP {r.status_code}")
        if not r.ok:
            raise CloudUnavailable(f"{self.provider} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]

    def complete(self, messages: list[dict]) -> str:
        return (self._chat(messages, None).get("content") or "").strip()

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn) -> str:
        msgs = list(messages)
        self.last_tool_calls = []
        nudged = False
        for _ in range(self.max_steps):
            msg = self._chat(msgs, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                if tools and not self.last_tool_calls and not nudged and CLAIMS_ACTION.search(reply):
                    nudged = True
                    msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": NUDGE}]
                    continue
                return reply
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                self.last_tool_calls.append(name)
                res = self.reg.execute(name, c["function"].get("arguments") or "{}", confirm)
                content = ("ERROR: " if res.error else "") + res.text
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": content})
        return "That took more steps than I'm allowed, so I stopped."


class Router:
    """Same interface as OllamaBrain. Local by default; cloud when configured, falling back to local."""

    def __init__(self, local, cloud: CloudBrain | None, registry: ToolRegistry, mode: str = "local",
                 cloud_in_gaming: bool = True):
        self.local, self.cloud, self.reg = local, cloud, registry
        self.mode = mode
        self.cloud_in_gaming = cloud_in_gaming
        self.private = False  # /private: force local for everything
        self.gaming = False
        self.last_tool_calls: list[str] = []
        self.last_backend = "local"

    @property
    def model(self) -> str:
        if self.cloud and self.cloud.available and self.mode == "cloud_first":
            return f"{self.local.model} + {self.cloud.provider}:{self.cloud.model}"
        return self.local.model

    def _use_cloud(self, tools: list[dict]) -> bool:
        if not (self.cloud and self.cloud.available) or self.private:
            return False
        names = {t["function"]["name"] for t in tools or []}
        if any((tool := self.reg.get(n)) and tool.group in PRIVATE_GROUPS for n in names):
            return False  # files / email stay on this PC
        return self.mode == "cloud_first" or (self.gaming and self.cloud_in_gaming)

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn) -> str:
        if self._use_cloud(tools):
            try:
                reply = self.cloud.respond(messages, tools, confirm)
                self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
                return reply
            except CloudUnavailable as e:
                if self.cloud.last_tool_calls:  # a tool already ran; don't repeat it locally
                    logger.warning("Cloud failed after running tools: {}", e)
                    return "I lost the connection to the cloud model midway. Please check whether it finished."
                logger.warning("Cloud unavailable ({}); using local model", e)
        reply = self.local.respond(messages, tools, confirm)
        self.last_tool_calls, self.last_backend = self.local.last_tool_calls, "local"
        return reply

    def complete(self, messages: list[dict]) -> str:
        return self.local.complete(messages)  # summaries contain personal history: keep local

    # pass-throughs to the local brain
    def check(self) -> None:
        self.local.check()

    def unload(self) -> None:
        self.local.unload()

    def set_cpu_only(self, cpu_only: bool) -> None:
        self.gaming = cpu_only
        self.local.set_cpu_only(cpu_only)
