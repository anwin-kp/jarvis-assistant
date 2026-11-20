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
        return [f"fact: {f[0]}" for f in facts] + [f"{ts} {role}: {text[:300]}" for ts, role, text in msgs]

    def profile(self, limit: int = 30) -> str:
        with self.lock:
            rows = self.db.execute("SELECT fact FROM facts ORDER BY id LIMIT ?", (limit,)).fetchall()
        return "\n".join(f"- {r[0]}" for r in rows)

    def audit(self, tool: str, args: dict, risk: str, status: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO audit_log(tool, args, risk, status) VALUES (?, ?, ?, ?)",
                (tool, json.dumps(args, default=str, ensure_ascii=False), risk, status),
            )
            self.db.commit()
