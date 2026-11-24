"""Your personal vocabulary: the names in your life, ranked, and used two ways.

1. The recogniser's prompt: your config words plus only the names you've SAID in turns JARVIS accepted, as one
   clean comma list. Measured on your recorded voice (20 phrases + a real 'Hey Jarvis'): a prompt full of names
   you never said (Spotify artists, playlists) gave the worst word error rate (19.5% vs 15.6%) and turned
   "Hey Jarvis" into "Kali Cervis"; even one name tacked on after "No." made it "Kali Jarvis" every time.
2. After recognition: a misheard name is corrected against the full list (no size limit). "Mixup Mesh" ->
   "Mix-Up Mash". Strict, so ordinary words are never "corrected" into names; each fix is counted, and
   names that keep being misheard move up in the prompt.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from pathlib import Path

from loguru import logger

PROMPT_CHARS = 700
# a source's base priority: what's most likely to be said and misheard
PRIOR = {"config": 100, "app": 6, "project": 5, "playlist": 4, "artist": 4, "fact": 3, "person": 3, "event": 2}
COMMON = set("""a an and are as at be but by can do for from go have he her his how i if in is it its me my no not
of on or our play please set she so stop that the their them then there they this to turn up us was we what when
where which who will with you your open close pause next yes okay ok song music volume what's it's that's
there's let's i'm i've don't can't won't""".split())

# "copy mobile" is a mishearing of "Quick OP Mobile", but "copy mobile folder" / "can you copy ..." can be a real
# command: a span starting with one of these verbs, where a command starts, is left alone
VERBS = {"copy", "call", "quit", "cut", "clear", "check", "cancel", "create"}
BEFORE_VERB = {"", "please", "you", "and", "then", "i", "we", "jarvis"}


def _fold(s: str) -> str:
    s = re.sub(r"[^a-z]", "", s.lower()).replace("ph", "f").replace("ck", "k")
    s = s.replace("qu", "k").replace("q", "k").replace("c", "k")  # one sound: "QCopy Mobile" ~ "Quick OP Mobile"
    s = s[:1] + s[1:].replace("y", "i")
    return re.sub(r"(.)\1+", r"\1", s)


class Lexicon:
    def __init__(self, stats_file: Path | None = None):
        self.terms: dict[str, dict] = {}  # term -> {"source", "private"}
        self.said: dict[str, int] = {}
        self.stats_file = stats_file
        self.fixes: dict[str, int] = {}
        self._lock = threading.Lock()
        if stats_file:
            try:
                self.fixes = json.loads(stats_file.read_text(encoding="utf-8")).get("fixes", {})
            except (OSError, ValueError):
                self.fixes = {}

    def add(self, terms, source: str, private: bool = False) -> None:
        for t in terms:
            t = re.sub(r"[^\w\s&'.,:()/-]", "", str(t))  # emoji / symbols only waste the recogniser's prompt
            t = re.sub(r"\s+", " ", t).strip(" .,;:-")
            if len(t) < 3 or t.lower() in COMMON or len(t) > 40:
                continue
            old = self.terms.get(t)
            if not old or PRIOR.get(source, 1) > PRIOR.get(old["source"], 1):
                self.terms[t] = {"source": source, "private": private and not (old and not old["private"])}
            elif old["private"] and not private:
                old["private"] = False

    def count_said(self, texts) -> None:
        """How often you've said each name (your own words: voice log and chat history)."""
        blob = " " + " ".join(t.lower() for t in texts if t) + " "
        self.said = {t: blob.count(" " + t.lower()) for t in self.terms}

    def score(self, t: str) -> float:
        # names you say and names that get misheard first; on a tie the shorter one (more names fit)
        return (PRIOR.get(self.terms[t]["source"], 1) + 3 * min(self.said.get(t, 0), 10) + 5 * self.fixes.get(t, 0)
                - len(t) / 1000)

    def prompt(self, base: str = "", cloud: bool = True, limit: int = 300) -> str:
        """Config words + names you've said (most said first), written as ONE comma list ending in a full stop:
        "Jarvis, Kochi, ..., Yes, No, Mix-Up Mash." Never names you haven't said (they made Whisper hear names)."""
        words = [w.strip() for w in re.split(r"[.,]\s*", base) if w.strip()]
        have = {w.lower() for w in words}
        for t in sorted((t for t in self.terms if self.said.get(t)), key=self.score, reverse=True):
            if t.lower() in have:
                continue
            if len(", ".join(words + [t])) + 1 > limit:
                break
            words.append(t)
            have.add(t.lower())
        return (", ".join(words) + ".") if words else ""

    # ------------------------------------------------------------------ after recognition
    last_fixed: list = []  # what the last correct() changed: counted only once the turn is accepted (count_fixes)

    def count_fixes(self, fixed: list) -> None:
        """Count corrections of an ACCEPTED turn. Counting every recording let a TV or song in the room 'train' the
        vocabulary: the same song credit was corrected again and again ('K. Sreekumar' -> 'M. G. Sreekumar')."""
        if not fixed:
            return
        with self._lock:
            for _, t in fixed:
                self.fixes[t] = self.fixes.get(t, 0) + 1
            self._save()

    def correct(self, text: str, count: bool = True) -> str:
        """Replace a misheard name with the right one (strict sound-alike match; ordinary words are left alone).
        count=False: remember the fixes in last_fixed for count_fixes() instead of counting them now."""
        self.last_fixed = []
        if not text or not self.terms:
            return text
        words = text.split()
        names = [(t, _fold(t), len(t.split())) for t in self.terms if len(_fold(t)) >= 5]
        i, out, fixed = 0, [], []
        def match(span: list[str]):
            """Best (ratio, term) for these words, or None."""
            raw = " ".join(span).strip(".,!?;:")
            core = [w.strip(".,!?;:").lower() for w in span]
            if len(span) == 1 and (raw.lower() in COMMON or len(raw) < 5):
                return None
            common = any(w in COMMON for w in core)  # "what's a" is never "WhatsApp", "and win" never "Anwin"
            f, found = _fold(raw), None
            for t, ft, tn in names:
                if raw.lower() == t.lower():
                    return (1.0, t)  # heard right: nothing to fix (but it is this name)
                if abs(tn - len(span)) > 1 or common:
                    continue
                r = difflib.SequenceMatcher(None, f, ft).ratio()
                if r >= (0.86 if len(span) == 1 or tn == 1 else 0.8) and (not found or r > found[0]):
                    found = (r, t)
            return found

        while i < len(words):
            best = None
            for n in (1, 2, 3, 4):  # the best-scoring span; on a tie the shorter one (names have up to 4 words)
                first = words[i].strip(".,!?;:").lower()
                if n > 1 and first in VERBS and (words[i - 1].strip(".,!?;:").lower() if i else "") in BEFORE_VERB:
                    continue
                if i + n <= len(words) and (m := match(words[i:i + n])) and (not best or m[0] > best[0] + 0.01):
                    best = (m[0], m[1], n, " ".join(words[i:i + n]).strip(".,!?;:"))
            if best and best[2] > 1:  # don't swallow a neighbour: "my mixup mesh" -> the name starts at "mixup"
                nxt = match(words[i + 1:i + best[2]]) if best[2] > 1 else None
                if nxt and nxt[1] == best[1] and nxt[0] >= best[0]:  # only if the shorter span is as good
                    best = None
            if best and best[3].lower() == best[1].lower():  # heard right: keep the words as they are
                out += words[i:i + best[2]]
                i += best[2]
            elif best:
                _, t, n, raw = best
                tail = words[i + n - 1][len(words[i + n - 1].rstrip(".,!?;:")):]  # keep the punctuation
                out.append(t + tail)
                fixed.append((raw, t))
                i += n
            else:
                out.append(words[i])
                i += 1
        if not fixed:
            return text  # untouched, spacing included
        logger.info("Corrected misheard names: {}", fixed)
        self.last_fixed = fixed
        if count:
            self.count_fixes(fixed)
        return " ".join(out)

    def _save(self) -> None:
        if self.stats_file:
            try:
                self.stats_file.write_text(json.dumps({"fixes": self.fixes}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
