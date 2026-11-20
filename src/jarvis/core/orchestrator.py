"""The turn loop: user text -> brain (+tools) -> reply, with history and memory."""

import datetime as dt
import re
import time

from loguru import logger

from jarvis.llm.tool_groups import pick_groups
from jarvis.memory.store import Memory
from jarvis.tools.registry import ConfirmFn, ToolRegistry

_REMEMBER = re.compile(
    r"^(?:(?:hey\s+)?jarvis[,\s]+)?(?:please\s+)?(?:can you\s+|could you\s+)?remember\s+(?:that\s+)?(?P<fact>.+)$",
    re.IGNORECASE,
)


_FILLER = re.compile(
    r"\s*(?:let me know|feel free to|is there anything else|if you need anything|anything else i can)[^.!?]*[.!?]?\s*$",
    re.IGNORECASE,
)


def strip_filler(reply: str) -> str:
    """Remove a trailing 'Let me know if you need anything else.' style sentence."""
    cleaned = _FILLER.sub("", reply).strip()
    return cleaned or reply


def explicit_remember_request(text: str) -> str | None:
    """'remember that I like tea' -> 'I like tea'. Ignores questions like 'remember when...?'."""
    m = _REMEMBER.match(text.strip())
    if not m:
        return None
    fact = m.group("fact").strip().rstrip(".!")
    if fact.endswith("?") or fact.lower().startswith(("when ", "what ", "how ", "where ", "who ", "me ")):
        return None
    return fact if len(fact) > 3 else None


class Orchestrator:
    def __init__(self, brain, registry: ToolRegistry, memory: Memory, persona: str, history_turns: int = 8,
                 idle_summary_minutes: float = 10):
        self.brain = brain
        self.reg = registry
        self.memory = memory
        self.persona = persona
        self.history_turns = history_turns
        self.history: list[dict] = []  # plain-text turns only
        self.idle_seconds = idle_summary_minutes * 60
        self.last_activity = time.monotonic()

    def system_prompt(self) -> str:
        prompt = self.persona
        if facts := self.memory.profile():
            prompt += f"\n\nKnown facts about the user:\n{facts}"
        if sums := self.memory.recent_summaries():
            prompt += "\n\nSummaries of recent earlier conversations:\n" + "\n".join(f"- {s}" for s in sums)
        return prompt

    def summarize_session(self) -> None:
        """Condense the current conversation into a stored summary, then start fresh."""
        if len(self.history) < 4:  # under two exchanges: not worth keeping
            self.history.clear()
            return
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in self.history)
        try:
            summary = self.brain.complete([
                {"role": "system", "content": "Summarize this conversation in 1-3 short sentences for future "
                 "reference. Keep names, decisions, open tasks and preferences. Output only the summary."},
                {"role": "user", "content": transcript},
            ])
            if summary:
                self.memory.add_summary(summary)
        except Exception:
            logger.exception("Session summary failed")
        self.history.clear()

    def reset(self) -> None:
        self.history.clear()

    def handle(self, text: str, confirm: ConfirmFn, on_sentence=None) -> str:
        if self.history and time.monotonic() - self.last_activity > self.idle_seconds:
            self.summarize_session()
        self.last_activity = time.monotonic()
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        user_msg = {"role": "user", "content": f"[Local time: {now}] {text}"}
        messages = [{"role": "system", "content": self.system_prompt()}, *self.history, user_msg]
        tools = self.reg.schemas(pick_groups(text))
        kw = {"on_sentence": on_sentence} if on_sentence else {}
        reply = strip_filler(self.brain.respond(messages, tools, confirm, **kw) or "(no reply)")

        # Safety net: small models sometimes say "remembered" without calling the tool.
        fact = explicit_remember_request(text)
        if fact and "remember" not in getattr(self.brain, "last_tool_calls", []):
            self.reg.execute("remember", {"fact": fact}, confirm)

        self.history += [user_msg, {"role": "assistant", "content": reply}]
        self.history = self.history[-2 * self.history_turns :]
        self.memory.log_turn(text, reply)
        return reply
