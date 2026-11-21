"""Task playbooks: how to do PC tasks correctly, retrieved before acting.

A playbook card (YAML) documents one task: what it does, the steps (tool calls), the tools, how to verify it, common
errors with recovery, and safety. Curated cards live in knowledge/tasks/ (reviewed, tested in CI); cards JARVIS learns
live in data/tasks/learned/ (private) and start as candidates.

Retrieval: cards are embedded by their title + example phrasings; the best clear match is put in the AI's context for
that turn ("follow this playbook"), and the find_task_playbook tool searches explicitly. Cards guide the AI; every
action still goes through the tools, their approvals and their result checks.

Learning (guarded): a multi-step task that finished with every tool ok, no WARNING/ERROR, and no correction from the
user becomes a candidate card; the same procedure succeeding again promotes it to verified. Dangerous tools and
failed/declined runs are never learned. Corrections given right after a playbook was used are attached to that card
as notes the AI must follow."""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from loguru import logger

from jarvis.core.config import DATA

CURATED = Path(__file__).resolve().parents[3] / "knowledge" / "tasks"
LEARNED = DATA / "tasks" / "learned"
NOTES = DATA / "tasks" / "notes"
REQUIRED = ("id", "title", "triggers", "what", "steps", "tools", "verify", "errors", "safety")
NEVER_LEARN = {"delete_file", "empty_recycle_bin", "kill_process", "shutdown_pc", "restart_pc", "send_draft",
               "run_powershell", "forget"}
CORRECTION = re.compile(r"^(no\b|nope|not that|i (said|meant|asked)|that'?s (not|wrong)|wrong|you (didn'?t|did not)|"
                        r"it (didn'?t|did not)|that didn'?t)", re.I)


@dataclass
class Card:
    data: dict
    path: Path
    notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def verified(self) -> bool:
        return self.data.get("status", "verified") == "verified"

    def render(self) -> str:
        """Compact text for the AI (a few hundred tokens)."""
        d = self.data
        lines = [f"TASK PLAYBOOK: {d['title']} [{d.get('status', 'verified')}, {d.get('source', 'curated')}]",
                 f"What: {d['what']}", "Steps:"]
        lines += [f"  {i}. {s}" for i, s in enumerate(d["steps"], 1)]
        lines.append("Verify: " + "; ".join(d["verify"]))
        if d.get("errors"):
            lines.append("If it goes wrong: " + "; ".join(f"{e['symptom']} -> {e['recovery']}" for e in d["errors"]))
        lines.append(f"Safety: {d['safety']}")
        if self.notes:
            lines.append("User corrections (always follow): " + "; ".join(self.notes[-5:]))
        return "\n".join(lines)


def validate(data: dict, tool_names: set[str] | None = None, learned: bool = False) -> list[str]:
    """Problems with a card (empty list = valid)."""
    errs = [f"missing '{k}'" for k in REQUIRED if not data.get(k)]
    if errs:
        return errs
    if not re.fullmatch(r"[a-z0-9-]+", data["id"]):
        errs.append("id must be lowercase-with-dashes")
    for key in ("triggers", "steps", "tools", "verify"):
        if not isinstance(data[key], list):
            errs.append(f"'{key}' must be a list")
    if tool_names is not None:
        errs += [f"unknown tool '{t}'" for t in data["tools"] if t not in tool_names]
    steps = " ".join(map(str, data["steps"]))
    errs += [f"tool '{t}' is listed but never used in steps" for t in data["tools"] if t not in steps]
    if learned and set(data["tools"]) & NEVER_LEARN:
        errs.append("learned cards may not use dangerous tools")
    for e in data.get("errors") or []:
        if not (isinstance(e, dict) and e.get("symptom") and e.get("recovery")):
            errs.append("each error needs 'symptom' and 'recovery'")
    return errs


def load_cards(folder: Path, tool_names: set[str] | None = None, learned: bool = False) -> list[Card]:
    cards = []
    for path in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            logger.warning("Playbook {} is not valid YAML: {}", path.name, e)
            continue
        if problems := validate(data or {}, tool_names, learned):
            logger.warning("Playbook {} skipped: {}", path.name, "; ".join(problems))
            continue
        card = Card(data, path)
        notes_file = NOTES / f"{card.id}.yaml"
        if notes_file.exists():
            card.notes = yaml.safe_load(notes_file.read_text(encoding="utf-8")) or []
        cards.append(card)
    return cards


class Playbooks:
    def __init__(self, embed, tool_names: set[str], threshold: float = 0.65, margin: float = 0.05):
        # measured: task requests match their card at 0.67-0.84, chat ("tell me a joke") stays under 0.5
        self.embed, self.tool_names, self.threshold, self.margin = embed, tool_names, threshold, margin
        self.cards: list[Card] = []
        self._vecs: np.ndarray | None = None
        self._owner: list[int] = []
        self.pending: dict | None = None  # last successful multi-step run, committed if not corrected
        self.last_used: str | None = None  # card id put in context for the last turn
        self.reload()

    # ------------------------------------------------------------------ index
    def reload(self) -> None:
        self.cards = load_cards(CURATED, self.tool_names) + load_cards(LEARNED, self.tool_names, learned=True)
        self._vecs = None

    def _index(self) -> None:
        texts, owner = [], []
        for i, c in enumerate(self.cards):
            for t in [c.data["title"], *c.data["triggers"]]:
                texts.append(f"search_document: {t}")
                owner.append(i)
        if not texts:
            self._vecs, self._owner = np.zeros((0, 1), np.float32), []
            return
        v = np.asarray(self.embed(texts), np.float32)
        self._vecs = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
        self._owner = owner

    def search(self, query: str, k: int = 3, verified_only: bool = False) -> list[tuple[Card, float]]:
        if self._vecs is None:
            self._index()
        if not len(self._owner):
            return []
        q = np.asarray(self.embed([f"search_query: {query}"])[0], np.float32)
        scores = self._vecs @ (q / (np.linalg.norm(q) + 1e-9))
        best: dict[int, float] = {}
        for i, s in zip(self._owner, scores, strict=True):
            best[i] = max(best.get(i, -1.0), float(s))
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        out = [(self.cards[i], s) for i, s in ranked if not verified_only or self.cards[i].verified]
        return out[:k]

    def for_prompt(self, query: str) -> str | None:
        """The playbook to follow for this request: only a verified card that clearly matches."""
        self.last_used = None
        if not query or len(query.split()) < 2:
            return None
        hits = self.search(query, k=2, verified_only=True)
        if not hits or hits[0][1] < self.threshold:
            return None
        if len(hits) > 1 and hits[0][1] - hits[1][1] < self.margin:
            return None  # no clear winner: let the AI choose (find_task_playbook)
        self.last_used = hits[0][0].id
        return hits[0][0].render()

    # ------------------------------------------------------------------ learning
    def observe(self, words: str, trace: list[tuple[str, dict, str, str]]) -> None:
        """After a turn: remember a clean multi-step success (committed if the next message isn't a correction)."""
        self.pending = None
        names = [t[0] for t in trace]
        clean = (len(trace) >= 2 and all(s == "ok" for _, _, s, _ in trace)
                 and not any(re.search(r"\b(WARNING|ERROR)\b", r or "") for *_, r in trace)
                 and not set(names) & NEVER_LEARN and words)
        if clean:
            self.pending = {"query": words, "trace": trace, "at": time.time()}

    def next_turn(self, words: str) -> None:
        """Called with the user's next message: a correction rejects the pending run and teaches the card."""
        corrected = bool(CORRECTION.match(words.strip()))
        if corrected and self.last_used:
            self.add_note(self.last_used, words.strip()[:200])
        if self.pending and not corrected and time.time() - self.pending["at"] < 600:
            self._commit(self.pending)
        self.pending = None

    def add_note(self, card_id: str, note: str) -> None:
        NOTES.mkdir(parents=True, exist_ok=True)
        path = NOTES / f"{card_id}.yaml"
        notes = (yaml.safe_load(path.read_text(encoding="utf-8")) or []) if path.exists() else []
        notes.append(note)
        path.write_text(yaml.safe_dump(notes[-10:], allow_unicode=True), encoding="utf-8")
        for c in self.cards:
            if c.id == card_id:
                c.notes = notes[-10:]
        logger.info("Playbook {} learned a correction: {}", card_id, note)

    def _commit(self, run: dict) -> None:
        names = [t[0] for t in run["trace"]]
        signature = list(dict.fromkeys(names))
        # same procedure already learned for a similar request: one more success
        for card, score in self.search(run["query"], k=3):
            if card.data.get("source") == "learned" and card.data["tools"] == signature and score >= 0.6:
                d = card.data
                d["successes"] = int(d.get("successes", 1)) + 1
                if run["query"] not in d["triggers"]:
                    d["triggers"] = (d["triggers"] + [run["query"]])[-8:]
                if d["successes"] >= 2 and d.get("status") != "verified":
                    d["status"] = "verified"
                    logger.info("Playbook {} verified after {} successes", d["id"], d["successes"])
                card.path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
                self._vecs = None
                return
        if any(c.data["tools"] == signature and c.data.get("source") != "learned"
               for c, s in self.search(run["query"], k=1) if s >= self.threshold):
            return  # a curated card already covers it
        LEARNED.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", run["query"].lower()).strip("-")[:40] or "task"
        data = {
            "id": f"learned-{slug}", "title": run["query"][:80], "status": "candidate", "source": "learned",
            "successes": 1, "triggers": [run["query"]],
            "what": f"Learned from a successful run: {run['query'][:120]}",
            "steps": [f"{n}({', '.join(f'{k}=...' for k in args)})" for n, args, _, _ in run["trace"]],
            "tools": signature,
            "verify": ["every step's result is ok, with no WARNING"],
            "errors": [{"symptom": "a step fails or warns", "recovery": "stop, tell the user which step failed"}],
            "safety": "learned procedure: the tools' own approvals still apply",
        }
        if validate(data, self.tool_names, learned=True):
            return
        path = LEARNED / f"{data['id']}.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.cards.append(Card(data, path))
        self._vecs = None
        logger.info("Learned a candidate playbook: {}", data["title"])


def register(reg, books: Playbooks) -> None:
    @reg.tool("find_task_playbook", "Look up how to do a PC task correctly (steps, tools, how to verify, what to do "
              "if it fails, safety) before doing an unfamiliar or multi-step task.",
              {"task": {"type": "string", "description": "the task in plain words"}},
              group="core", read_only=True)
    def find_task_playbook(task: str):
        hits = [(c, s) for c, s in books.search(task, k=3) if s >= 0.55]
        if not hits:
            return "No playbook for that yet: use your tools carefully and verify the result."
        return "\n\n".join(c.render() + ("" if c.verified else "\n(UNVERIFIED: learned once; double-check results)")
                           for c, _ in hits)
