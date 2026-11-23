"""Optional free-tier cloud brain (Groq / Google Gemini / OpenRouter) via their OpenAI-compatible APIs,
plus the router that decides local vs cloud. Off by default: JARVIS stays 100% local unless you add a key."""

import re
import threading

import requests
from loguru import logger

from jarvis.core.config import get_secret
from jarvis.llm.ollama_brain import acted, result_reply, stopped_reply, unbacked_claim
from jarvis.tools.registry import ConfirmFn, ToolRegistry

PROVIDERS = {  # base URL, keyring entry name
    "groq": ("https://api.groq.com/openai/v1", "groq_api_key"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini_api_key"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter_api_key"),
}
# never sent to the cloud ("tabs": your tab titles show what you browse)
PRIVATE_GROUPS = {"files", "email", "docs", "screen", "clipboard", "notes", "tabs"}
# Requests that DO something: in smart mode these go to the bigger cloud model, which picks tools and follows
# multi-step instructions far more reliably than the small local one (wrong tools, claimed actions, loops).
ACTION_GROUPS = {"system", "media", "browser", "windows", "pc", "reminder", "home", "code"}


class CloudUnavailable(RuntimeError):
    retry_after: float = 60.0  # seconds until this provider/model can be tried again


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
        self.cancel = threading.Event()  # the Router shares the local brain's: "stop" ends a cloud turn too

    @property
    def name(self) -> str:  # each Groq model has its own free per-minute budget: cool down per model
        return f"{self.provider}/{self.model}"

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _chat(self, messages: list[dict], tools: list[dict] | None, on_text=None) -> dict:
        """One request. on_text: stream the answer, calling on_text(piece) as it arrives (speech starts with the
        first sentence instead of waiting for the whole reply: cloud turns had a 4.7 s median to the first word)."""
        body = {"model": self.model, "messages": messages, "temperature": 0.3, **self.extra}
        if tools:
            body["tools"] = tools
        if on_text:
            body["stream"] = True
        try:
            r = requests.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.key}"}, stream=bool(on_text))
        except requests.RequestException as e:
            raise CloudUnavailable(f"network: {e}") from e
        self._check(r)
        if on_text:
            return self._read_stream(r, on_text)
        try:
            data = r.json()
            msg = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as err:  # a garbled answer must not crash the turn
            e = CloudUnavailable(f"{self.name}: unexpected answer ({err})")
            e.retry_after = 0.0
            raise e from err
        if u := data.get("usage"):
            logger.debug("Cloud {}: {} prompt + {} reply tokens", self.name, u.get("prompt_tokens"),
                         u.get("completion_tokens"))
        return msg

    def _read_stream(self, r, on_text) -> dict:
        """Server-sent events (OpenAI format): text deltas go to on_text while no tool call has started; tool-call
        pieces (name, arguments) are joined by index. A connection drop after text was spoken keeps that text."""
        import json

        content, calls = "", {}
        r.encoding = "utf-8"  # event streams declare no charset: requests guessed Latin-1 ("28Â°C"), seen live
        try:
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    choices = json.loads(payload).get("choices") or []
                except ValueError:
                    continue
                delta = (choices[0].get("delta") or {}) if choices else {}
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)),
                                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                if piece := delta.get("content"):
                    content += piece
                    if not calls:
                        on_text(piece)
                if self.cancel.is_set():
                    break
        except requests.RequestException as err:
            if not content or calls:
                e = CloudUnavailable(f"{self.name}: stream dropped ({err})")
                e.retry_after = 0.0
                raise e from err
            logger.warning("Cloud {}: stream dropped after {} chars; keeping what was said", self.name, len(content))
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [{**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
        return msg

    def _check(self, r) -> None:
        if r.status_code == 429 or r.status_code >= 500:
            e = CloudUnavailable(f"{self.name} HTTP {r.status_code}")
            try:  # Groq says exactly when its per-minute token budget refills (often a few seconds)
                e.retry_after = min(60.0, max(2.0, float(r.headers.get("retry-after", 60))))
            except (TypeError, ValueError):
                pass
            raise e
        if not r.ok:  # a bad request (e.g. Groq's "tool_use_failed"): try the next model, but no cooldown
            e = CloudUnavailable(f"{self.provider} HTTP {r.status_code}: {r.text[:200]}")
            e.retry_after = 0.0
            raise e

    def complete(self, messages: list[dict]) -> str:
        return (self._chat(messages, None).get("content") or "").strip()

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn, on_sentence=None,
                prior_tools: list[str] | None = None) -> str:
        """prior_tools: tools another provider already ran in this turn (their results are in messages)."""
        msgs = list(messages)
        self.last_tool_calls = list(prior_tools or [])
        nudged = False
        for _ in range(self.max_steps):
            if self.cancel.is_set():
                return stopped_reply(self.last_tool_calls)
            stream = None
            if on_sentence and not nudged:  # speak sentences as they arrive; claims wait for the checked answer
                from jarvis.llm.ollama_brain import SentenceStream

                stream = SentenceStream(on_sentence, lambda: acted(self.reg, self.last_tool_calls))
            try:
                msg = self._chat(msgs, tools, stream.feed if stream else None)
            except CloudUnavailable as e:
                if self.last_tool_calls:  # tools already ran: hand over the conversation, results included
                    e.partial = msgs
                    e.results = [m["content"] for m in msgs if m.get("role") == "tool"]
                raise
            calls = msg.get("tool_calls") or []
            if stream and not calls:
                stream.finish()
            if not calls:
                reply = (msg.get("content") or "").strip()
                nudge = unbacked_claim(reply, acted(self.reg, self.last_tool_calls)) if tools and not nudged else None
                if nudge:
                    nudged = True
                    msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": nudge}]
                    continue
                return reply
            if self.cancel.is_set():  # interrupted while the request was out: don't run what it asked for
                return stopped_reply(self.last_tool_calls)
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                if self.cancel.is_set():
                    return stopped_reply(self.last_tool_calls)
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


EXAMPLES = "Examples of choosing tools well:"


def trim_examples(messages: list[dict], offered: set[str], known: set[str]) -> list[dict]:
    """Cloud requests offer ~10 tools, but the persona's examples cover ~25 (2.4k of its 4.2k characters, resent
    on every step). Keep an example if it names an offered tool or names no tool at all (general advice)."""
    if not messages or messages[0].get("role") != "system" or EXAMPLES not in (messages[0].get("content") or ""):
        return messages
    text = messages[0]["content"]
    head, _, rest = text.partition(EXAMPLES)
    lines = rest.split("\n")
    end = next((i for i, ln in enumerate(lines[1:], 1) if not ln.startswith(("-", " "))), len(lines))
    bullets: list[list[str]] = []
    for ln in lines[1:end]:
        if ln.startswith("-") or not bullets:
            bullets.append([ln])
        else:
            bullets[-1].append(ln)  # an indented continuation line belongs to the bullet above
    kept = []
    for b in bullets:
        named = {w for w in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", " ".join(b)) if w in known}
        if not named or named & offered:
            kept += b
    body = "\n".join([lines[0], *kept, *lines[end:]]) if kept else "\n".join(lines[end:])
    return [{**messages[0], "content": head + (EXAMPLES + body if kept else body.lstrip("\n"))}, *messages[1:]]


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

        return [b for b in self.brains if b.available and self.cooldown.get(b.name, 0) < time.monotonic()]

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
                logger.warning("Cloud {} unavailable ({}); trying the next one", b.name, e)
                self.cooldown[b.name] = time.monotonic() + e.retry_after
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
                kw = {"on_sentence": on_sentence} if on_sentence else {}
                out = b.respond(msgs, tools, confirm, prior_tools=done, **kw)
                self.last_tool_calls = b.last_tool_calls
                return out
            except CloudUnavailable as e:
                logger.warning("Cloud {} unavailable ({}){}", b.name, e,
                               "; continuing on the next one from the tool results" if b.last_tool_calls else "")
                self.cooldown[b.name] = time.monotonic() + e.retry_after
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
        if cloud and hasattr(local, "cancel"):
            for b in getattr(cloud, "brains", None) or [cloud]:
                b.cancel = local.cancel  # one "stop" for whichever brain is working

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
        tried_cloud = self._use_cloud(tools, think, private, action)
        if tried_cloud:
            try:
                return self._cloud_turn(messages, tools, confirm, cloud_tools, on_sentence)
            except CloudUnavailable as e:
                if self.cloud.last_tool_calls:  # a tool already ran; don't repeat it locally
                    logger.warning("Cloud failed after running tools: {}", e)
                    self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
                    return result_reply(getattr(e, "results", None))
                logger.warning("Cloud unavailable ({}); using local model", e)
        kw = {"on_sentence": on_sentence} if on_sentence else {}
        if think:
            kw["think"] = True
        try:
            reply = self.local.respond(personal_context(messages, True), tools, confirm, **kw)
        except Exception as e:
            # Ollama stopped or crashed mid-session: a turn that may use the cloud is answered there instead of
            # failing ("Something went wrong"). Never after a local tool ran (it would run twice), never private.
            ran = getattr(self.local, "last_tool_calls", None)
            if tried_cloud or ran or not self._cloud_allowed(tools, private):
                raise
            logger.warning("Local model failed ({}); answering with the cloud model", e)
            try:
                return self._cloud_turn(messages, tools, confirm, cloud_tools, on_sentence)
            except CloudUnavailable as ce:
                if self.cloud.last_tool_calls:
                    self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
                    return result_reply(getattr(ce, "results", None))
                raise e from ce
        self.last_tool_calls, self.last_backend = self.local.last_tool_calls, "local"
        return reply

    def _cloud_allowed(self, tools: list[dict], private: bool | None) -> bool:
        if not (self.cloud and self.cloud.available) or self.private:
            return False
        return not (private if private is not None else any(self._is_private(t) for t in tools or []))

    def _cloud_turn(self, messages, tools, confirm, cloud_tools, on_sentence=None) -> str:
        if hasattr(self.local, "cancel"):
            self.local.cancel.clear()
        offered = cloud_tools if cloud_tools is not None else tools
        public_tools = [t for t in offered or [] if not self._is_private(t)]  # never private tools
        msgs = recent(personal_context(messages, self.share_personal))
        msgs = trim_examples(msgs, {t["function"]["name"] for t in public_tools}, set(self.reg.names()))
        kw = {"on_sentence": on_sentence} if on_sentence else {}
        reply = self.cloud.respond(msgs, public_tools, confirm, **kw)
        self.last_tool_calls, self.last_backend = self.cloud.last_tool_calls, "cloud"
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
