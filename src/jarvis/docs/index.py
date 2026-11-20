"""Ask your documents: local semantic search over PDFs, Word, text and Markdown files.
Embeddings come from a small local model (nomic-embed-text via Ollama); everything stays on this PC."""

import os
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from loguru import logger

from jarvis.tools.registry import ToolRegistry
from jarvis.tools.web_tools import untrusted

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_files(path TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS doc_chunks(id INTEGER PRIMARY KEY, path TEXT, chunk INTEGER, text TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS doc_chunks_path ON doc_chunks(path);
"""
EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "__pycache__", "AppData", "$RECYCLE.BIN"}
MAX_FILE_MB = 25


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages[:300])
    if ext == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return ""


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # prefer to cut at a sentence end
            cut = text.rfind(". ", start + size // 2, end)
            end = cut + 1 if cut != -1 else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def ollama_embedder(client, model: str) -> Callable[[list[str]], np.ndarray]:
    pulled = []

    def embed(texts: list[str]) -> np.ndarray:
        if not pulled:
            names = {m.model for m in client.list().models}
            if model not in names and f"{model}:latest" not in names:
                logger.info("Downloading embedding model {}", model)
                client.pull(model)
            pulled.append(True)
        return np.asarray(client.embed(model=model, input=texts).embeddings, dtype=np.float32)

    return embed


class DocIndex:
    def __init__(self, memory, folders: list[Path], embed: Callable[[list[str]], np.ndarray]):
        self.db, self.lock = memory.db, memory.lock
        self.folders, self.embed = folders, embed
        with self.lock:
            self.db.executescript(SCHEMA)
        self._matrix: np.ndarray | None = None
        self._rows: list[tuple[str, str]] = []
        self.indexing = threading.Lock()

    def _files(self):
        for root in self.folders:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for f in filenames:
                    p = Path(dirpath) / f
                    if p.suffix.lower() in EXTENSIONS and not f.startswith("~$"):
                        yield p

    def index(self) -> str:
        """Incremental: only new or changed files are (re)embedded; deleted files are dropped."""
        if not self.indexing.acquire(blocking=False):
            return "Indexing is already running."
        try:
            with self.lock:
                known = dict(self.db.execute("SELECT path, mtime FROM doc_files").fetchall())
            seen, added, failed = set(), 0, 0
            for p in self._files():
                key = str(p)
                seen.add(key)
                try:
                    st = p.stat()
                    if st.st_size > MAX_FILE_MB * 2**20 or known.get(key) == st.st_mtime:
                        continue
                    chunks = chunk_text(extract_text(p))
                    vecs = [self.embed([f"search_document: {c}" for c in chunks[i:i + 32]])
                            for i in range(0, len(chunks), 32)]
                    with self.lock:
                        self.db.execute("DELETE FROM doc_chunks WHERE path=?", (key,))
                        for i, (c, v) in enumerate(zip(chunks, np.concatenate(vecs) if vecs else [], strict=False)):
                            self.db.execute("INSERT INTO doc_chunks(path, chunk, text, emb) VALUES (?,?,?,?)",
                                            (key, i, c, np.asarray(v, np.float32).tobytes()))
                        self.db.execute("INSERT OR REPLACE INTO doc_files(path, mtime) VALUES (?,?)",
                                        (key, st.st_mtime))
                        self.db.commit()
                    added += 1
                except Exception as e:
                    failed += 1
                    logger.debug("Could not index {}: {}", p, e)
            gone = [k for k in known if k not in seen]
            with self.lock:
                for k in gone:
                    self.db.execute("DELETE FROM doc_chunks WHERE path=?", (k,))
                    self.db.execute("DELETE FROM doc_files WHERE path=?", (k,))
                self.db.commit()
            self._matrix = None
            msg = f"Indexed {added} new/changed file(s), removed {len(gone)}, {failed} unreadable."
            logger.info(msg)
            return msg
        finally:
            self.indexing.release()

    def _load(self) -> None:
        with self.lock:
            rows = self.db.execute("SELECT path, text, emb FROM doc_chunks").fetchall()
        self._rows = [(p, t) for p, t, _ in rows]
        if rows:
            m = np.stack([np.frombuffer(e, dtype=np.float32) for _, _, e in rows])
            self._matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        else:
            self._matrix = np.zeros((0, 1), np.float32)

    def search(self, query: str, k: int = 5) -> list[dict]:
        if self._matrix is None:
            self._load()
        if not len(self._rows):
            return []
        q = self.embed([f"search_query: {query}"])[0]
        scores = self._matrix @ (q / (np.linalg.norm(q) + 1e-9))
        best = np.argsort(-scores)[:k]
        return [{"file": Path(self._rows[i][0]).name, "path": self._rows[i][0], "score": round(float(scores[i]), 3),
                 "text": self._rows[i][1]} for i in best]

    def start_background(self) -> None:
        threading.Thread(target=self.index, daemon=True, name="doc-index").start()


def register(reg: ToolRegistry, idx: DocIndex) -> None:
    @reg.tool("search_documents", "Answer questions from the user's own documents (PDF, Word, text). "
              "Returns the most relevant passages with file names.",
              {"question": {"type": "string"}}, group="docs", read_only=True)
    def search_documents(question: str):
        hits = idx.search(question)
        if not hits:
            return "No indexed documents yet (indexing may still be running)."
        return untrusted("documents", "\n\n".join(f"[{h['file']}] {h['text']}" for h in hits))

    @reg.tool("index_documents", "Re-scan the document folders for new or changed files.", {}, group="docs")
    def index_documents():
        return idx.index()
