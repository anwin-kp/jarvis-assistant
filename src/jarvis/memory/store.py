"""Local SQLite memory: conversation log, user facts, audit log."""

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), role TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;

CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), fact TEXT UNIQUE);

CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), summary TEXT);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')),
  tool TEXT, args TEXT, risk TEXT, status TEXT);
"""


class Memory:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def log_turn(self, user: str, reply: str) -> None:
        with self.lock:
            self.db.executemany(
                "INSERT INTO messages(role, text) VALUES (?, ?)",
                [("user", user), ("assistant", reply)],
            )
            self.db.commit()

    def remember(self, fact: str) -> str:
        fact = fact.strip()
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO facts(fact) VALUES (?)", (fact,))
            self.db.commit()
        return "Saved." if cur.rowcount else "I already knew that."

    def forget(self, fact_id: int) -> str:
        with self.lock:
            cur = self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self.db.commit()
        return "Forgotten." if cur.rowcount else f"No fact with id {fact_id}."

    def list_facts(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, fact FROM facts ORDER BY id").fetchall()
        return [{"id": i, "fact": f} for i, f in rows]

    def recall(self, query: str, limit: int = 8) -> list[str]:
        words = [w for w in query.replace('"', " ").split() if len(w) > 2] or [query.replace('"', " ")]
        fts = " OR ".join(f'"{w}"' for w in words)
        with self.lock:
            facts = self.db.execute(
                "SELECT fact FROM facts WHERE " + " OR ".join("fact LIKE ?" for _ in words) + " LIMIT ?",
                [f"%{w}%" for w in words] + [limit],
            ).fetchall()
            msgs = self.db.execute(
                "SELECT m.ts, m.role, m.text FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts, limit),
            ).fetchall()
            sums = self.db.execute(
                "SELECT ts, summary FROM summaries WHERE " + " OR ".join("summary LIKE ?" for _ in words) + " LIMIT 3",
                [f"%{w}%" for w in words],
            ).fetchall()
        return (
            [f"fact: {f[0]}" for f in facts]
            + [f"summary {ts}: {s}" for ts, s in sums]
            + [f"{ts} {role}: {text[:300]}" for ts, role, text in msgs]
        )

    def semantic_recall(self, query: str, k: int = 5) -> list[str]:
        """Facts and conversation summaries ranked by meaning (needs self.embed)."""
        embed = getattr(self, "embed", None)
        if embed is None:
            return []
        import numpy as np

        with self.lock:
            items = [f"fact: {r[0]}" for r in self.db.execute("SELECT fact FROM facts ORDER BY id DESC LIMIT 200")]
            items += [f"summary {ts}: {s}" for ts, s in
                      self.db.execute("SELECT ts, summary FROM summaries ORDER BY id DESC LIMIT 200")]
        if not items:
            return []
        cache = self.__dict__.setdefault("_vec_cache", {})
        new = [t for t in items if t not in cache]
        if new:
            for t, v in zip(new, embed([f"search_document: {t}" for t in new]), strict=True):
                cache[t] = v / (np.linalg.norm(v) + 1e-9)
        q = embed([f"search_query: {query}"])[0]
        q = q / (np.linalg.norm(q) + 1e-9)
        scored = sorted(items, key=lambda t: -float(cache[t] @ q))
        return [t for t in scored[:k] if float(cache[t] @ q) > 0.5]

    # ------------------------------------------------------------ learning about the user
    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
            self.db.commit()

    def messages_after(self, after_id: int, limit: int = 16) -> list[tuple[int, str, str]]:
        with self.lock:
            return self.db.execute("SELECT id, role, text FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                                   (after_id, limit)).fetchall()

    def _similar_fact(self, fact: str, threshold: float = 0.9) -> int | None:
        """id of an existing fact that says the same thing (by meaning), if any."""
        facts = self.list_facts()
        low = fact.lower().strip(" .")
        for f in facts:
            if f["fact"].lower().strip(" .") == low:
                return f["id"]
        embed = getattr(self, "embed", None)
        if embed is None or not facts:
            return None
        import numpy as np

        vecs = np.asarray(embed([f"search_document: {f['fact']}" for f in facts] + [f"search_document: {fact}"]))
        vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
        scores = vecs[:-1] @ vecs[-1]
        best = int(np.argmax(scores))
        return facts[best]["id"] if float(scores[best]) >= threshold else None

    def learn(self, fact: str) -> bool:
        """Add a fact unless one with the same meaning exists. Returns True if added."""
        fact = " ".join(fact.split())
        if len(fact) < 6 or self._similar_fact(fact) is not None:
            return False
        return self.remember(fact) == "Saved."

    def replace_fact(self, fact_id: int, fact: str) -> bool:
        with self.lock:
            cur = self.db.execute("UPDATE OR IGNORE facts SET fact=?, ts=datetime('now','localtime') WHERE id=?",
                                  (" ".join(fact.split()), fact_id))
            self.db.commit()
        return bool(cur.rowcount)

    def profile(self, query: str = "", core: int = 25, relevant: int = 10) -> str:
        """What the AI is told about the user: everything while it's short; otherwise the core facts
        (who/where/work/people/preferences) plus the ones most related to the current message."""
        facts = self.list_facts()
        if len(facts) > core + relevant:
            core_cats = ("[identity]", "[people]", "[places]", "[work]", "[preferences]")
            chosen = [f for f in facts if f["fact"].lower().startswith(core_cats)][-core:]
            picked = {f["id"] for f in chosen}
            if query and getattr(self, "embed", None):
                for hit in self.semantic_recall(query, k=relevant + len(picked)):
                    f = next((f for f in facts if f["fact"] == hit.removeprefix("fact: ")), None)
                    if f and f["id"] not in picked and len(chosen) < core + relevant:
                        chosen.append(f)
                        picked.add(f["id"])
            facts = chosen or facts[-(core + relevant):]
        return "\n".join(f"- {f['fact']}" for f in facts)

    def add_summary(self, summary: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO summaries(summary) VALUES (?)", (summary.strip(),))
            self.db.commit()

    def recent_summaries(self, limit: int = 3) -> list[str]:
        with self.lock:
            rows = self.db.execute("SELECT ts, summary FROM summaries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [f"{ts}: {s}" for ts, s in reversed(rows)]

    def export(self) -> dict:
        out = {}
        with self.lock:
            for table in ("facts", "summaries", "messages", "audit_log"):
                cur = self.db.execute(f"SELECT * FROM {table}")
                cols = [c[0] for c in cur.description]
                out[table] = [dict(zip(cols, row, strict=True)) for row in cur]
        return out

    def audit(self, tool: str, args: dict, risk: str, status: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO audit_log(tool, args, risk, status) VALUES (?, ?, ?, ?)",
                (tool, json.dumps(args, default=str, ensure_ascii=False), risk, status),
            )
            self.db.commit()
