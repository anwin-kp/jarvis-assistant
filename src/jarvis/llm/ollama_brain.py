"""Local LLM brain using Ollama's tool calling."""

import re

import ollama
from loguru import logger

from jarvis.tools.registry import ConfirmFn, ToolRegistry

CLAIMS_ACTION = re.compile(
    r"\b(taken|written|typed|played|paused|resumed|switched|snapped|minimi[sz]ed|maximi[sz]ed|copied|sent|"
    r"enabled|disabled|added|saved|created|scheduled|opened|closed|deleted|removed|moved|renamed|muted|unmuted|remembered|"
    r"noted|cancell?ed|marked|launched|started|stopped|locked|set (?:to|for|up)|turned (?:on|off|up|down)|"
    r"has been|have been|is now|are now)\b",
    re.IGNORECASE,
)
NUDGE = (
    "[System check] Your reply says an action happened, but you did not call any tool, so nothing was done. "
    "If the user asked for an action, call the right tool now. If no action was needed, repeat your answer."
)


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
FILLER = re.compile(r"^\s*(let me know|feel free to|is there anything else|if you need anything)", re.I)


class SentenceStream:
    """Collects streamed text and hands complete sentences to `emit` as soon as they're finished.
    Holds back sentences that claim an action before any tool ran (they may be bluffs)."""

    def __init__(self, emit, tools_ran):
        self.emit, self.tools_ran = emit, tools_ran
        self.buf, self.held = "", False

    def feed(self, text: str) -> None:
        self.buf += text
        parts = SENTENCE_END.split(self.buf)
        for sentence in parts[:-1]:
            self._out(sentence)
        self.buf = parts[-1]

    def _out(self, sentence: str) -> None:
        s = sentence.strip()
        if not s or self.held or FILLER.match(s):
            return
        if not self.tools_ran() and CLAIMS_ACTION.search(s):
            self.held = True  # let the final (checked) answer be spoken instead
            return
        self.emit(s)

    def finish(self) -> None:
        self._out(self.buf)
        self.buf = ""


class BrainUnavailable(RuntimeError):
    pass


class OllamaBrain:
    def __init__(self, registry: ToolRegistry, cfg: dict):
        self.reg = registry
        self.client = ollama.Client(host=cfg["ollama_host"])
        self.model = cfg["ollama_model"]
        self.think = cfg.get("think", False)
        self.keep_alive = cfg.get("keep_alive", "10m")
        self.options = {"num_ctx": cfg.get("num_ctx", 8192), "temperature": cfg.get("temperature", 0.3)}
        self.max_steps = cfg.get("max_steps", 6)

    def check(self) -> None:
        """Raise BrainUnavailable with a helpful message if Ollama or the model is missing."""
        try:
            models = [m.model for m in self.client.list().models]
        except Exception as e:
            raise BrainUnavailable(
                "Can't reach Ollama. Start it from the Start menu (or run `ollama serve`)."
            ) from e
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in models:
            raise BrainUnavailable(f"Model {self.model} isn't downloaded. Run: ollama pull {self.model}")

    def unload(self) -> None:
        """Free the GPU memory immediately (e.g. before gaming)."""
        self.client.generate(model=self.model, prompt="", keep_alive=0)

    def set_cpu_only(self, cpu_only: bool) -> None:
        """Gaming mode: run the model on the CPU so the GPU stays free (slower replies)."""
        if cpu_only:
            self.options["num_gpu"] = 0
        else:
            self.options.pop("num_gpu", None)

    def complete(self, messages: list[dict]) -> str:
        """Plain completion without tools (summaries etc.)."""
        resp = self.client.chat(model=self.model, messages=messages, think=False,
                                keep_alive=self.keep_alive, options=self.options)
        return (resp.message.content or "").strip()

    def _chat_stream(self, msgs, tools, think, on_sentence):
        """Streaming chat: speaks sentences while the model is still writing. Returns a Message."""
        content, calls = "", []
        stream = SentenceStream(on_sentence, lambda: bool(self.last_tool_calls))
        for chunk in self.client.chat(model=self.model, messages=msgs, tools=tools or None, think=think,
                                      keep_alive=self.keep_alive, options=self.options, stream=True):
            m = chunk.message
            if m.tool_calls:
                calls.extend(m.tool_calls)
            if m.content:
                content += m.content
                if not calls:
                    stream.feed(m.content)
        if not calls:
            stream.finish()
        return ollama.Message(role="assistant", content=content, tool_calls=calls or None)

    def warmup(self, messages: list[dict], tools: list[dict]) -> None:
        """Load the model and pre-fill its prompt cache with the system prompt + tools."""
        self.client.chat(model=self.model, messages=messages, tools=tools or None, think=False,
                         keep_alive=self.keep_alive, options={**self.options, "num_predict": 1})

    def respond(self, messages: list[dict], tools: list[dict], confirm: ConfirmFn, on_sentence=None,
                think: bool = False) -> str:
        """messages: plain [{"role", "content"}] dicts. Tool calls live only inside this turn."""
        msgs = list(messages)
        self.last_tool_calls: list[str] = []
        nudged = False
        for step in range(self.max_steps):
            think_now = self.think or nudged or think  # think harder on complex requests / the retry
            if on_sentence and not nudged:
                msg = self._chat_stream(msgs, tools, think_now, on_sentence)
            else:
                msg = self.client.chat(model=self.model, messages=msgs, tools=tools or None, think=think_now,
                                       keep_alive=self.keep_alive, options=self.options).message
            msgs.append(msg)
            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                # Small models sometimes claim an action without calling a tool: push back once.
                if tools and not self.last_tool_calls and not nudged and CLAIMS_ACTION.search(reply):
                    nudged = True
                    logger.info("Reply claims an action but no tool ran; nudging: {}", reply[:80])
                    msgs.append({"role": "user", "content": NUDGE})
                    continue
                if nudged and not self.last_tool_calls and CLAIMS_ACTION.search(reply):
                    reply += " (Heads-up: no tool actually ran, so nothing was changed. Please ask again.)"
                return reply
            for call in msg.tool_calls:
                name, args = call.function.name, call.function.arguments
                logger.debug("step {} tool call {} {}", step, name, args)
                self.last_tool_calls.append(name)
                result = self.reg.execute(name, args, confirm)
                msgs.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": ("ERROR: " if result.error else "") + result.text,
                })
        return "That took more steps than I'm allowed, so I stopped. Want me to try a simpler approach?"
