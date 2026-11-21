"""The turn loop: user text -> brain (+tools) -> reply, with history and memory."""

import datetime as dt
import re
import threading
import time

from loguru import logger

from jarvis.llm.semantic import needs_thinking
from jarvis.llm.tool_groups import pick_groups
from jarvis.memory.store import Memory
from jarvis.tools.registry import ConfirmFn, ToolRegistry

_REMEMBER = re.compile(
    r"^(?:(?:hey\s+)?jarvis[,\s]+)?(?:please\s+)?(?:can you\s+|could you\s+)?remember\s+(?:that\s+)?(?P<fact>.+)$",
    re.IGNORECASE,
)


PERSONAL_START, PERSONAL_END = "<<personal>>", "<</personal>>"
INTERVIEW = """GET-TO-KNOW-YOU MODE is on: you are getting to know the user so you can help them better.
- Ask ONE short, friendly question at a time about something not already in what you know about them. Topics, in
  order: what to call them; where they live and work or study; their job or studies; their daily routine (wake,
  work, sleep times); important people (family, partner, friends, colleagues: names); music and artists they like;
  food they like or avoid; apps and tools they use most; hobbies; current goals or projects; how they want you
  to talk (short or detailed, formal or casual); anything else they want you to know.
- After each answer: call remember with one clear fact in third person starting with a category in brackets
  (e.g. "[people] Has a sister named Anu."), react in a few words, then ask the next question.
- Skip a topic when the user doesn't want to answer. Never ask for passwords, card numbers or health details.
- When the user says stop / that's enough, or the topics are done: thank them, say in one sentence what you
  learned, and call end_interview."""


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
        self.semantic = None  # SemanticGroups
        self.location_fn = None  # () -> "City, Region, Country" (cached) or None
        self.last_think = False
        self.idle_seconds = idle_summary_minutes * 60
        self.last_activity = time.monotonic()
        self.interview = False  # "get to know me" mode
        self.lock = threading.RLock()  # a turn and the idle summary never touch the history at the same time

    def system_prompt(self, query: str = "") -> str:
        """Persona + what JARVIS knows about the user. The personal part is wrapped in PERSONAL markers so
        the router can leave it out of anything sent to the cloud (memory.share_with_cloud)."""
        personal = []
        if self.location_fn and (where := self.location_fn()):
            personal.append(f"The user is currently in or near: {where} (approximate).")
        if facts := self.memory.profile(query):
            personal.append("What you know about the user (use it naturally, without being asked; categories "
                            "in brackets; [lessons] are corrections the user gave you: always follow them):\n"
                            f"{facts}")
        if sums := self.memory.recent_summaries():
            personal.append("Summaries of recent earlier conversations:\n" + "\n".join(f"- {s}" for s in sums))
        prompt = self.persona
        if personal and hasattr(self.brain, "cloud"):  # router: it strips or unwraps the block
            prompt += f"\n\n{PERSONAL_START}\n" + "\n\n".join(personal) + f"\n{PERSONAL_END}"
        elif personal:
            prompt += "\n\n" + "\n\n".join(personal)
        if self.interview:
            prompt += "\n\n" + INTERVIEW
        return prompt

    def cloud_tools(self, words: str, tools: list[dict], k: int = 10) -> list[dict]:
        """A much smaller tool list for the cloud model (Groq's free tier: 8k tokens/minute, and every step resends
        everything). The big model needs no help choosing: the tools of the playbook in use + the k closest by
        meaning. Measured: 15-44 tools (1-3.2k tokens) -> about 10 (~0.7k)."""
        by_name = {t["function"]["name"]: t for t in tools}
        keep: list[str] = []
        books = getattr(self.reg, "playbooks", None)
        if books and books.last_used:
            card = next((c for c in books.cards if c.id == books.last_used), None)
            keep += [n for n in (card.data["tools"] if card else []) if n in by_name]
        if self.semantic:
            keep += [n for n in self.semantic.rank_tools(words, k * 3) if n in by_name][:k]
        keep = list(dict.fromkeys(keep))
        if len(keep) < 3:  # ranking unavailable: don't risk leaving the model without tools
            return tools
        return [by_name[n] for n in keep]

    def summarize_session(self) -> None:
        """Condense the current conversation into a stored summary, then start fresh."""
        with self.lock:
            self._summarize()

    def summarize_if_idle(self) -> None:
        """Called by the learner when JARVIS has been quiet: keeps the summary even if JARVIS restarts later."""
        if self.history and time.monotonic() - self.last_activity > self.idle_seconds:
            self.summarize_session()

    def _summarize(self) -> None:
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

    def add_exchange(self, text: str, reply: str) -> None:
        """Record a turn that was answered without the AI (instant commands)."""
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        self.history += [{"role": "user", "content": f"[Local time: {now}] {text}"},
                         {"role": "assistant", "content": reply}]
        self.history = self.history[-2 * self.history_turns :]
        self.last_activity = time.monotonic()
        self.memory.log_turn(text, reply)

    def reset(self) -> None:
        self.history.clear()

    def handle(self, text: str, confirm: ConfirmFn, on_sentence=None, route_text: str | None = None,
               private: bool = False) -> str:
        """route_text: the user's own words, when text also carries attachment content - tools, thinking and
        privacy are judged from what the user asked, not from what's inside a file. private: keep it local."""
        with self.lock:
            return self._handle(text, confirm, on_sentence, route_text, private)

    def _handle(self, text, confirm, on_sentence, route_text, private) -> str:
        if self.history and time.monotonic() - self.last_activity > self.idle_seconds:
            self._summarize()
        self.last_activity = time.monotonic()
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        user_msg = {"role": "user", "content": f"[Local time: {now}] {text}"}
        words = text if route_text is None else route_text
        messages = [{"role": "system", "content": self.system_prompt(words)}, *self.history, user_msg]
        books = getattr(self.reg, "playbooks", None)
        if books:
            books.next_turn(words)  # a correction now teaches the playbook used last turn
            try:
                playbook = books.for_prompt(words)
            except Exception as e:  # embedding model unavailable: work without
                logger.debug("Playbook lookup failed: {}", e)
                playbook = None
            if playbook:
                messages[0]["content"] += "\n\n" + playbook + "\nFollow this playbook, then verify as it says."
            self.reg.trace = []
        asked = pick_groups(words)  # what the words explicitly ask for
        groups = set(asked)
        if self.interview:
            groups.add("interview")  # end_interview stays available while getting to know the user
        if self.semantic and words:
            groups |= self.semantic.pick(words)
        tools = self.reg.schemas(groups)
        if books and books.last_used:  # the playbook is already in context: looking it up again only wastes a step
            tools = [t for t in tools if t["function"]["name"] != "find_task_playbook"]
        self.last_think = needs_thinking(words)
        kw = {"on_sentence": on_sentence} if on_sentence else {}
        if self.last_think:
            kw["think"] = True
        if hasattr(self.brain, "cloud"):
            kw["cloud_tools"] = self.cloud_tools(words, tools)
        if hasattr(self.brain, "cloud"):  # router: privacy follows what you asked for, not tools added "just in case"
            from jarvis.llm.cloud_brain import PRIVATE_GROUPS

            kw["private"] = private or bool(asked & PRIVATE_GROUPS)
            from jarvis.llm.cloud_brain import ACTION_GROUPS

            kw["action"] = bool(asked & ACTION_GROUPS)
        try:
            reply = strip_filler(self.brain.respond(messages, tools, confirm, **kw) or "(no reply)")
        finally:
            if books:
                books.observe(words, self.reg.trace or [])
                self.reg.trace = None

        # Safety net: small models sometimes say "remembered" without calling the tool.
        fact = explicit_remember_request(text)
        if fact and "remember" not in getattr(self.brain, "last_tool_calls", []):
            self.reg.execute("remember", {"fact": fact}, confirm)

        self.history += [user_msg, {"role": "assistant", "content": reply}]
        self.history = self.history[-2 * self.history_turns :]
        self.memory.log_turn(text, reply)
        return reply
