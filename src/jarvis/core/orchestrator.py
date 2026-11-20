"""The turn loop: user text -> brain (+tools) -> reply, with history and memory."""

import datetime as dt
import re

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
    def __init__(self, brain, registry: ToolRegistry, memory: Memory, persona: str, history_turns: int = 8):
        self.brain = brain
        self.reg = registry
        self.memory = memory
        self.persona = persona
        self.history_turns = history_turns
        self.history: list[dict] = []  # plain-text turns only

    def system_prompt(self) -> str:
        facts = self.memory.profile()
        return self.persona + (f"\n\nKnown facts about the user:\n{facts}" if facts else "")

    def reset(self) -> None:
        self.history.clear()

    def handle(self, text: str, confirm: ConfirmFn) -> str:
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        user_msg = {"role": "user", "content": f"[Local time: {now}] {text}"}
        messages = [{"role": "system", "content": self.system_prompt()}, *self.history, user_msg]
        tools = self.reg.schemas(pick_groups(text))
        reply = strip_filler(self.brain.respond(messages, tools, confirm) or "(no reply)")

        # Safety net: small models sometimes say "remembered" without calling the tool.
        fact = explicit_remember_request(text)
        if fact and "remember" not in getattr(self.brain, "last_tool_calls", []):
            self.reg.execute("remember", {"fact": fact}, confirm)

        self.history += [user_msg, {"role": "assistant", "content": reply}]
        self.history = self.history[-2 * self.history_turns :]
        self.memory.log_turn(text, reply)
        return reply
