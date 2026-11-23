"""Local LLM brain using Ollama's tool calling."""

import re
import threading

import ollama
from loguru import logger

from jarvis.tools.registry import ConfirmFn, ToolRegistry

_DONE = (r"(?:taken|written|typed|played|paused|resumed|switched|snapped|minimi[sz]ed|maximi[sz]ed|copied|sent|"
         r"enabled|disabled|added|saved|stored|created|scheduled|opened|closed|deleted|removed|moved|renamed|muted|"
         r"unmuted|remembered|noted|cancell?ed|marked|launched|started|stopped|locked|set|turned|changed|updated|"
         r"emailed|booked|installed|pasted|merged|cleared|organi[sz]ed|renamed|zipped|extracted|arranged|"
         r"switched|updated|fixed)")
# A claim that JARVIS itself just did something: "I've paused the music", "Done - saved.", "The timer is now set".
# Not ordinary prose ("Kerala's history was written...", "the war started in 1914"), which used to trigger a
# needless second (thinking, non-streamed) model call and held back speech.
_FILLER = r"(?:(?!(?:can|could|will|would|should|may|might|shall|to|not|never)\b)\w+ )"  # no offers/negations
# "I'll check the clipboard for you. One moment." - a promise with no tool call: nothing will happen.
_DO = (r"(?:check|look|lower|raise|reduce|increase|dim|brighten|turn|set|switch|open|close|play|pause|resume|"
       r"skip|search|find|read|send|create|add|delete|remove|start|stop|take|copy|save|remember|adjust|change|"
       r"mute|unmute|enable|disable|launch|schedule|remind|note|write|type|move|rename|lock|get|fetch|pull up)")
PROMISES = re.compile(rf"\bI(?:'ll| will|'m going to| am going to)\s+(?:now |just |quickly |go ahead and )?{_DO}\b"
                      rf"|\b(?:let me(?! know)|one moment|just a moment|give me a (?:second|moment))\b", re.IGNORECASE)
_DOING = (r"(?:closing|opening|launching|starting|stopping|playing|pausing|resuming|muting|unmuting|setting|"
          r"turning|switching|locking|sending|saving|deleting|moving|minimi[sz]ing|maximi[sz]ing|skipping)")
CLAIMS_ACTION = re.compile(
    rf"(?:\bI(?:'ve| have| just| also)?\b|\bdone\b[,.!: -]+(?:I've |I )?)\s*{_FILLER}{{0,2}}{_DONE}\b"
    rf"|\b(?:has|have) (?:now |just |successfully )?been {_DONE}\b"
    rf"|\b(?:is|are) now (?:\w+ )?{_DONE}\b"
    rf"|\bsuccessfully {_DONE}\b"
    # an action in progress, said as the whole answer: "Closing notepad.", "Okay, opening Spotify now."
    rf"|^\s*(?:(?:ok(?:ay)?|sure|alright|right away)[,.!]?\s*)?{_DOING}\b"
    # a bare past-tense report as the opening words: "Opened quickop-mobile in VS Code.", "Copied the mobile folder
    # to your clipboard.", "Pasted it into VS Code." (live: three of these with no tool at all slipped through)
    rf"|^\s*(?:(?:ok(?:ay)?|sure|done|alright)[,.!]?\s*)?{_DONE}\b(?! (?:by|in \d{{4}}|on \w+day))"
    # "WhatsApp Web is open in Chrome." with no tool at all
    r"|\b(?:is|are) (?:now )?(?:open|opened|running|closed|playing) (?:in|on) (?:google )?(?:chrome|edge|brave|"
    r"firefox|vs ?code|spotify|your browser|the browser)\b"
    # reporting what an app/screen shows without having looked ("The display shows 84")
    rf"|\b(?:the )?(?:display|screen|window|app|calculator|page) (?:now )?(?:shows|says|displays|reads)\b",
    re.IGNORECASE,
)
NUDGE = (
    "[System check] Your reply says an action happened, but you did not call any tool, so nothing was done. "
    "If the user asked for an action, call the right tool now. If no action was needed, repeat your answer."
)
NUDGE_PROMISE = (
    "[System check] You said you would do something, but you did not call a tool, so nothing will happen. "
    "Call the right tool now. If no tool fits, answer the user directly instead."
)


NUDGE_LIVE = (
    "[System check] This question needs live data that only a tool has ({tools}). You answered without calling it, "
    "so your answer may be made up or out of date. Call the tool now, then answer only from its result."
)


def pending(must_use, ran) -> set:
    """Required tools (live data) of which none has run yet in this turn."""
    must = set(must_use or ())
    return must if must and not must & set(ran) else set()


def acted(reg, tool_names: list[str]) -> bool:
    """Did any tool that CHANGES something run? Listing emails doesn't back "I've marked them as read"."""
    return any((t := reg.get(n)) is not None and not t.read_only for n in tool_names)


def unbacked_claim(reply: str, tools_ran: bool = False) -> str | None:
    """The nudge to send when a reply claims or promises an action that no tool call backs up. After tools ran,
    a claim may be true, but a promise ("Let me play it now. One moment") still means nothing more will happen."""
    if not tools_ran and CLAIMS_ACTION.search(reply):
        return NUDGE
    if PROMISES.search(reply):
        return NUDGE_PROMISE
    return None


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
        if not self.tools_ran() and unbacked_claim(s):
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
        self.options = {"num_ctx": cfg.get("num_ctx", 8192), "temperature": cfg.get("temperature", 0.3),
                        # Ollama memory-maps the model file: with every layer on the GPU, Windows kept that file in
                        # RAM as well (measured: 7.9 GB -> 0.9 GB for qwen3:8b without it, same speed). Sent with
                        # every request: a request without it would reload the model the mapped way.
                        "use_mmap": cfg.get("use_mmap", False)}
        self.max_steps = cfg.get("max_steps", 6)
        self.cancel = threading.Event()  # set when you interrupt: stop generating

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
            if self.cancel.is_set():
                break
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
                think: bool = False, must_use=None) -> str:
        """messages: plain [{"role", "content"}] dicts. Tool calls live only inside this turn. must_use: tools whose
        live data the answer needs (temperatures, open tabs, date maths...): answering without one gets pushed back,
        and nothing is spoken until one has run (a live test heard invented temperatures and tabs)."""
        msgs = list(messages)
        self.last_tool_calls: list[str] = []
        self.cancel.clear()
        nudged = live_nudged = False
        results: list[str] = []
        for step in range(self.max_steps):
            if self.cancel.is_set():
                return stopped_reply(self.last_tool_calls)
            think_now = self.think or nudged or think  # think harder on complex requests / the retry
            if on_sentence and not nudged and not pending(must_use, self.last_tool_calls):
                msg = self._chat_stream(msgs, tools, think_now, on_sentence)
            else:
                msg = self.client.chat(model=self.model, messages=msgs, tools=tools or None, think=think_now,
                                       keep_alive=self.keep_alive, options=self.options).message
            if self.cancel.is_set():  # you interrupted while it was writing: don't run what it asked for
                return stopped_reply(self.last_tool_calls)
            msgs.append(msg)
            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                # Small models sometimes claim an action without calling a tool: push back once.
                nudge = unbacked_claim(reply, acted(self.reg, self.last_tool_calls)) if tools and not nudged else None
                if nudge:
                    nudged = True
                    logger.info("Reply claims/promises an action but no tool ran; nudging: {}", reply[:80])
                    msgs.append({"role": "user", "content": nudge})
                    continue
                if (need := pending(must_use, self.last_tool_calls)) and not live_nudged:
                    live_nudged = True
                    logger.info("Answered without the live-data tool {}; nudging: {}", sorted(need), reply[:80])
                    msgs.append({"role": "user", "content": NUDGE_LIVE.format(tools=", ".join(sorted(need)))})
                    continue
                if nudged and not self.last_tool_calls and CLAIMS_ACTION.search(reply):
                    reply += " (Heads-up: no tool actually ran, so nothing was changed. Please ask again.)"
                if not reply and results:  # tools ran but the model said nothing: "(no reply)" helped nobody
                    return result_reply(results)
                return reply
            for call in msg.tool_calls:
                if self.cancel.is_set():
                    return stopped_reply(self.last_tool_calls)
                name, args = call.function.name, call.function.arguments
                logger.debug("step {} tool call {} {}", step, name, args)
                self.last_tool_calls.append(name)
                result = self.reg.execute(name, args, confirm)
                content = ("ERROR: " if result.error else "") + result.text
                results.append(content)
                msgs.append({"role": "tool", "tool_name": name, "content": content})
        return "That took more steps than I'm allowed, so I stopped. Want me to try a simpler approach?"


def stopped_reply(ran: list[str]) -> str:
    """You interrupted: say so plainly (and what had already run), so the history never claims more."""
    done = ", ".join(dict.fromkeys(ran))
    return f"Stopped (already done: {done})." if done else "Stopped."


def result_reply(results: list[str] | None) -> str:
    """The answer when the AI couldn't finish but a tool did: its own (checked) result, plainly."""
    last = next((r for r in reversed(results or []) if r and r.strip()), "")
    if not last:
        return "I did that step, but couldn't finish my answer (my cloud brain is busy). Please check it worked."
    text = " ".join(last.replace("<untrusted", "").split())[:300]
    return text if not text.startswith("ERROR") else "That didn't work: " + text[6:].strip(": ")
