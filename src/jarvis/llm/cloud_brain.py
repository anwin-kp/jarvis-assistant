"""Optional free-tier cloud brain (Groq / Google Gemini / OpenRouter) via their OpenAI-compatible APIs,
plus the router that decides local vs cloud. Off by default: JARVIS stays 100% local unless you add a key."""

import requests
from loguru import logger

from jarvis.core.config import get_secret
from jarvis.llm.ollama_brain import acted, unbacked_claim
from jarvis.tools.registry import ConfirmFn, ToolRegistry

PROVIDERS = {  # base URL, keyring entry name
    "groq": ("https://api.groq.com/openai/v1", "groq_api_key"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini_api_key"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter_api_key"),
}
PRIVATE_GROUPS = {"files", "email", "docs", "screen", "clipboard", "notes"}  # never sent to the cloud
# Requests that DO something: in smart mode these go to the bigger cloud model, which picks tools and follows
# multi-step instructions far more reliably than the small local one (wrong tools, claimed actions, loops).
ACTION_GROUPS = {"system", "media", "browser", "windows", "pc", "reminder", "home"}


class CloudUnavailable(RuntimeError):
    pass


class CloudBrain:
    def __init__(self, registry: ToolRegistry, provider: str, model: str, max_steps: int = 6, timeout: float = 25,
                 extra: dict | None = None):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}")
        self.base, key_name = PROVIDERS[provider]
        self.key = get_secret(key_name)
        self.reg, self.provider, self.model = registry, provider, model
        self.max_steps, self.timeout = max_steps, timeout
        self.extra = extra or {}  # provider-specific request options, e.g. {"reasoning_effort": "low"}
        self.last_tool_calls: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": 0.3, **self.extra}
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

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn, on_sentence=None,
                prior_tools: list[str] | None = None) -> str:
        """prior_tools: tools another provider already ran in this turn (their results are in messages)."""
        msgs = list(messages)
        self.last_tool_calls = list(prior_tools or [])
        nudged = False
        for _ in range(self.max_steps):
            try:
                msg = self._chat(msgs, tools)
            except CloudUnavailable as e:
                if self.last_tool_calls:  # tools already ran: hand over the conversation, results included
                    e.partial = msgs
                    e.results = [m["content"] for m in msgs if m.get("role") == "tool"]
                raise
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                nudge = unbacked_claim(reply, acted(self.reg, self.last_tool_calls)) if tools and not nudged else None
                if nudge:
                    nudged = True
                    msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": nudge}]
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


def personal_context(messages: list[dict], keep: bool) -> list[dict]:
    """The system prompt's <<personal>> block (facts, summaries, location): kept (markers removed) or dropped."""
    from jarvis.core.orchestrator import PERSONAL_END, PERSONAL_START

    out = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "system" and isinstance(c, str) and PERSONAL_START in c:
            head, rest = c.split(PERSONAL_START, 1)
            body, tail = rest.split(PERSONAL_END, 1) if PERSONAL_END in rest else (rest, "")
            kept = "\n\n" + body.strip() if keep else ""
            m = {**m, "content": (head.rstrip() + kept + tail).strip()}
        out.append(m)
    return out


def recent(messages: list[dict], turns: int = 3) -> list[dict]:
    """System prompt + the last few exchanges: every cloud step resends the whole conversation."""
    head = [m for m in messages[:1] if m.get("role") == "system"]
    body = messages[len(head):]
    return head + body[-(2 * turns + 1):]


def as_text_history(messages: list[dict]) -> list[dict]:
    """Hand a half-done turn to another provider: tool calls and results become plain text (Gemini rejects tool-call
    records written by another model: "missing a thought_signature"). It reads what already ran and finishes."""
    import json

    out, done = [], []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                done.append(f"{c['function']['name']}({c['function'].get('arguments') or ''})")
        elif m.get("role") == "tool":
            done[-1:] = [f"{done[-1]} -> {m.get('content', '')[:600]}"] if done else []
        else:
            out.append(m)
    if done:
        out.append({"role": "user", "content": "[System] Already done in this turn (do NOT run these again): "
                    + json.dumps(done, ensure_ascii=False) + ". Continue from here: answer the user, or take the next "
                    "step only if one is still needed."})
    return out


def result_reply(results: list[str] | None) -> str:
    """The answer when the AI couldn't finish but a tool did: its own (checked) result, plainly."""
    last = next((r for r in reversed(results or []) if r and r.strip()), "")
    if not last:
        return "I did that step, but couldn't finish my answer (my cloud brain is busy). Please check it worked."
    text = " ".join(last.replace("<untrusted", "").split())[:300]
    return text if not text.startswith("ERROR") else "That didn't work: " + text[6:].strip(": ")


class CloudChain:
    """Several free cloud brains tried in order (Groq, then Gemini...). A provider that is rate limited or down is
    skipped for a minute; once a tool has run, the turn is not retried elsewhere (it would run the tool twice)."""

    def __init__(self, brains: list["CloudBrain"]):
        self.brains = brains
        self.cooldown: dict[str, float] = {}
        self.last_tool_calls: list[str] = []
        self.active = brains[0] if brains else None

    @property
    def available(self) -> bool:
        return any(b.available for b in self.brains)

    @property
    def provider(self) -> str:
        return "+".join(b.provider for b in self.brains if b.available)

    @property
    def model(self) -> str:
        return (self.active or self.brains[0]).model

    def _ready(self):
        import time

        return [b for b in self.brains if b.available and self.cooldown.get(b.provider, 0) < time.monotonic()]

    def _try(self, call):
        import time

        last: Exception | None = None
        for b in self._ready():
            self.active = b
            try:
                out = call(b)
                self.last_tool_calls = b.last_tool_calls
                return out
            except CloudUnavailable as e:
                self.last_tool_calls = b.last_tool_calls
                if b.last_tool_calls:
                    raise
                logger.warning("Cloud {} unavailable ({}); trying the next one", b.provider, e)
                self.cooldown[b.provider] = time.monotonic() + 60
                last = e
        raise CloudUnavailable(str(last) if last else "no cloud provider available")

    def respond(self, messages, tools, confirm, on_sentence=None) -> str:
        """Groq's free per-minute limit often hits mid-task (after a tool ran). The next provider then continues
        from the conversation so far, tool results included, so finished actions are never repeated."""
        import time

        msgs, done, results, last = messages, [], [], None
        for b in self._ready():
            self.active = b
            try:
                out = b.respond(msgs, tools, confirm, prior_tools=done)
                self.last_tool_calls = b.last_tool_calls
                return out
            except CloudUnavailable as e:
                logger.warning("Cloud {} unavailable ({}){}", b.provider, e,
                               "; continuing on the next one from the tool results" if b.last_tool_calls else "")
                self.cooldown[b.provider] = time.monotonic() + 60
                if getattr(e, "partial", None):
                    msgs, done, results = as_text_history(e.partial), list(b.last_tool_calls), e.results
                elif b.last_tool_calls:  # tools ran but there's nothing to continue from: never risk running them twice
                    self.last_tool_calls = b.last_tool_calls
                    raise
                last = e
        self.last_tool_calls = done
        err = CloudUnavailable(str(last) if last else "no cloud provider available")
        err.results = results
        raise err

    def complete(self, messages) -> str:
        return self._try(lambda b: b.complete(messages))


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
        self.share_personal = False  # memory.share_with_cloud: send what JARVIS knows about you to the cloud model

    @property
    def model(self) -> str:
        if self.cloud and self.cloud.available and self.mode in ("cloud_first", "smart"):
            return f"{self.local.model} + {self.cloud.provider}:{self.cloud.model}"
        return self.local.model

    def _is_private(self, tool_schema: dict) -> bool:
        tool = self.reg.get(tool_schema["function"]["name"])
        return bool(tool and tool.group in PRIVATE_GROUPS)

    def _use_cloud(self, tools: list[dict], complex_: bool = False, private: bool | None = None,
                   action: bool = False) -> bool:
        if not (self.cloud and self.cloud.available) or self.private:
            return False
        if private is None:  # caller didn't say: judge by the tools on offer
            private = any(self._is_private(t) for t in tools or [])
        if private:
            return False  # files / email / screen / clipboard / notes stay on this PC
        if self.mode == "smart" and (complex_ or action):
            return True  # hard questions and actions go to the bigger free model
        return self.mode == "cloud_first" or (self.gaming and self.cloud_in_gaming)

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn, on_sentence=None,
                think: bool = False, private: bool | None = None, action: bool = False,
                cloud_tools: list[dict] | None = None) -> str:
        if self._use_cloud(tools, think, private, action):
            try:
                offered = cloud_tools if cloud_tools is not None else tools
                public_tools = [t for t in offered or [] if not self._is_private(t)]  # never private tools
                msgs = recent(personal_context(messages, self.share_personal))
                reply = self.cloud.respond(msgs, public_tools, confirm)
                self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
                return reply
            except CloudUnavailable as e:
                if self.cloud.last_tool_calls:  # a tool already ran; don't repeat it locally
                    logger.warning("Cloud failed after running tools: {}", e)
                    self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
                    return result_reply(getattr(e, "results", None))
                logger.warning("Cloud unavailable ({}); using local model", e)
        kw = {"on_sentence": on_sentence} if on_sentence else {}
        if think:
            kw["think"] = True
        reply = self.local.respond(personal_context(messages, True), tools, confirm, **kw)
        self.last_tool_calls, self.last_backend = self.local.last_tool_calls, "local"
        return reply

    def complete(self, messages: list[dict]) -> str:
        return self.local.complete(messages)

    def warmup(self, messages: list[dict], tools: list[dict]) -> None:
        self.local.warmup(personal_context(messages, True), tools)  # personal history: keep local

    # pass-throughs to the local brain
    def check(self) -> None:
        self.local.check()

    def unload(self) -> None:
        self.local.unload()

    def set_cpu_only(self, cpu_only: bool) -> None:
        self.gaming = cpu_only
        self.local.set_cpu_only(cpu_only)
