"""Getting to know the user: while JARVIS is idle, the LOCAL model reads new conversation turns and keeps a
profile of lasting facts (people, places, work, routine, likes...). Nothing here is ever sent to the cloud.

Progress is stored (meta 'learned_upto'), so past conversations are learned once and restarts lose nothing.
Also writes a session summary once the conversation has gone quiet, instead of waiting for the next message."""

import json
import re
import threading
import time
from collections.abc import Callable

from loguru import logger

CATEGORIES = ("identity", "people", "places", "work", "routine", "preferences", "music", "food", "tech", "goals",
              "dates", "other")

PROMPT = """You keep a profile of the user of a personal assistant called JARVIS.
From the conversation below, extract NEW lasting facts about the user: who they are, people in their life (name +
relation), home/work places, job or studies, routine and schedule, preferences and dislikes (music, food, apps,
how they like answers), devices and setup, goals and ongoing projects, important dates.

Rules:
- Only what the USER said or clearly confirmed about themselves. Never facts about the world, never what the
  assistant said, never guesses. Skip one-off requests ("play X", "weather in Y") unless they show a lasting
  preference (asked for the same artist repeatedly, says "my favourite...", "I always...").
- Requests to JARVIS are not facts about the user: never write "asks for...", "has a habit of asking...",
  "wanted to know...". Test phrases and questions about documents are not facts either.
- Asking about something is not a fact about the user: weather in a city is not where they live, a song they
  asked for once is not their taste. They must say it ("I live in...", "I love...", "my sister...").
- Skip passwords, card numbers, email addresses, phone numbers, health conditions and other secrets.
- Each fact: one short third-person sentence starting with a category in brackets, e.g.
  "[music] Likes Yuvan Shankar Raja's songs." Categories: {cats}.
- Every fact needs "evidence": the exact words from a User line that show it. No User line, no fact (what
  JARVIS said, e.g. emails or search results it read out, is never evidence).
- If a fact updates an existing one (moved, new job, changed preference), put it in "replace" with that id.
- Already known facts must not be added again.

Known facts (id: fact):
{facts}

Conversation:
{convo}

Answer with JSON only (empty lists if nothing new):
{{"add": [{{"fact": "[category] ...", "evidence": "exact user words"}}],
  "replace": [{{"id": 0, "fact": "[category] ...", "evidence": "exact user words"}}]}}"""

_CAT = re.compile(r"^(?:\[(\w+)\]|(\w+):)\s*")  # "[music] ..." or "music: ..."


def flatten(items) -> list:
    """The model sometimes nests its answer (["[x] ...", ["[y] ..."]]): one flat list of strings/dicts."""
    out: list = []
    for item in items if isinstance(items, list) else ([items] if items else []):
        out.extend(flatten(item) if isinstance(item, list) else [item])
    return out


# The user talking about themselves: a fact needs this in its evidence.
FIRST_PERSON = re.compile(r"\b(i|i m|im|i ve|i d|i ll|me|my|mine|myself|we|our|ours|us)\b")
STOP = {"the", "and", "user", "their", "they", "has", "have", "with", "for", "likes", "like", "prefers", "prefer",
        "is", "are", "was", "who", "that", "this", "from", "into", "about", "named", "called", "works", "lives"}
SECRET = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{7,}\d")  # emails, phone numbers


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def supported(item: dict, said: str) -> bool:
    """The quoted evidence really is (mostly) in what the user said: guards against invented facts."""
    ev = _norm(str(item.get("evidence", "")))
    if len(ev) < 3:
        return False
    if not FIRST_PERSON.search(ev):  # "what's the weather in Goa" says nothing about the user
        return False
    fact_words = {w for w in _norm(_CAT.sub("", str(item.get("fact", "")))).split() if len(w) > 2} - STOP
    if not fact_words & set(ev.split()):  # the quote must be about the fact ("can you see my screen" =/= Spotify)
        return False
    told = _norm(said)
    if ev in told:
        return True
    words, vocab = ev.split(), set(told.split())
    return len(words) >= 3 and sum(w in vocab for w in words) / len(words) >= 0.8


def clean_fact(fact: str) -> str | None:
    if not isinstance(fact, str):
        return None
    fact = " ".join(str(fact).split()).strip()
    m = _CAT.match(fact)
    cat = (m.group(1) or m.group(2)).lower() if m else ""
    if m and cat not in CATEGORIES and not m.group(1):
        m, cat = None, ""  # "Note: ..." is not a category
    body = fact[m.end():] if m else fact
    fact = f"[{cat if cat in CATEGORIES else 'other'}] {body}"
    if len(body) < 6 or len(body) > 220 or SECRET.search(body):
        return None
    return fact


class Learner:
    def __init__(self, memory, chat: Callable[[str], str], is_busy: Callable[[], bool],
                 summarize: Callable[[], None] | None = None, idle_s: float = 15, summary_idle_s: float = 600):
        self.memory, self.chat, self.is_busy = memory, chat, is_busy
        self.summarize, self.idle_s, self.summary_idle_s = summarize, idle_s, summary_idle_s
        self.last_activity = time.monotonic()
        self.stop = threading.Event()
        self.on_learned: Callable[[list[str]], None] | None = None  # e.g. HUD notice

    def touch(self) -> None:
        """Call on every user turn: learning waits for a quiet moment so it never slows a reply."""
        self.last_activity = time.monotonic()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="learner").start()

    def _loop(self) -> None:
        while not self.stop.wait(5):
            idle = time.monotonic() - self.last_activity
            if self.is_busy() or idle < self.idle_s:
                continue
            try:
                did = False
                # keep going while still quiet (a backlog of past conversations finishes in ~20 s, not minutes)
                while not self.stop.is_set() and not self.is_busy() and \
                        time.monotonic() - self.last_activity >= self.idle_s and self.step():
                    did = True
                if not did and self.summarize and idle > self.summary_idle_s:
                    self.summarize()
            except Exception:
                logger.exception("Learning step failed")
                self.stop.wait(60)

    def step(self, batch: int = 16) -> bool:
        """Learn from the next unread turns. Returns True if there was something to read."""
        upto = int(self.memory.get_meta("learned_upto", "0") or 0)
        rows = self.memory.messages_after(upto, batch)
        if not rows:
            return False
        # JARVIS's replies are only short context: facts must come from the user's own words
        convo = "\n".join(f"User: {text[:500]}" if role == "user" else f"(JARVIS replied: {text[:150]})"
                          for _, role, text in rows)
        said = " ".join(text for _, role, text in rows if role == "user")
        if said.strip():
            self.apply(self.extract(convo), said)
        self.memory.set_meta("learned_upto", rows[-1][0])
        return True

    def extract(self, convo: str) -> dict:
        facts = "\n".join(f"{f['id']}: {f['fact']}" for f in self.memory.list_facts()[-80:]) or "(none yet)"
        raw = self.chat(PROMPT.format(cats=", ".join(CATEGORIES), facts=facts, convo=convo))
        try:
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except ValueError:
            logger.debug("Learner: not JSON: {}", raw[:200])
            return {}
        return data if isinstance(data, dict) else {}

    def apply(self, data: dict, said: str) -> list[str]:
        learned = []
        known = {f["id"] for f in self.memory.list_facts()}
        for item in flatten(data.get("replace")):
            if (isinstance(item, dict) and item.get("id") in known and supported(item, said)
                    and (fact := clean_fact(item.get("fact", "")))):
                if self.memory.replace_fact(int(item["id"]), fact):
                    learned.append(fact)
        for item in flatten(data.get("add")):
            if not (isinstance(item, dict) and supported(item, said)):
                logger.debug("Learner: dropped unsupported {}", item)
                continue
            if (fact := clean_fact(item.get("fact", ""))) and self.memory.learn(fact):
                learned.append(fact)
        if learned:
            logger.info("Learned about the user: {}", learned)
            if self.on_learned:
                self.on_learned(learned)
        return learned


def ollama_chat(brain) -> Callable[[str], str]:
    """A private, local JSON call on the same model (never the cloud)."""
    local = getattr(brain, "local", brain)

    def chat(prompt: str) -> str:
        r = local.client.chat(model=local.model, messages=[{"role": "user", "content": prompt}], think=False,
                              format="json", keep_alive=local.keep_alive,
                              options={**local.options, "temperature": 0})
        return r.message.content or ""

    return chat
