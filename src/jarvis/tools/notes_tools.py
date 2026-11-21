"""Notes / journal as Markdown files in Documents\\JARVIS Notes (yours to open, sync and edit)."""

import re
from datetime import datetime
from pathlib import Path

from jarvis.tools.registry import ToolRegistry


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "", name).strip()[:60] or "note"


def register(reg: ToolRegistry, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)

    @reg.tool("take_note", "Save a note. Without a title it goes into today's journal file.",
              {"text": {"type": "string"}, "title": {"type": "string", "description": "optional"}},
              required=["text"], group="notes")
    def take_note(text: str, title: str = ""):
        now = datetime.now()
        if title:
            path = folder / f"{now:%Y-%m-%d} {_safe(title)}.md"
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                f.write((f"# {title}\n\n" if new else "\n") + text.strip() + "\n")
        else:
            path = folder / f"{now:%Y-%m-%d}.md"
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                f.write((f"# Journal {now:%A %d %B %Y}\n\n" if new else "") + f"- {now:%H:%M} {text.strip()}\n")
        return f"Noted in {path.name}."

    @reg.tool("search_notes", "Search your notes for words.", {"query": {"type": "string"}}, group="notes",
              read_only=True)
    def search_notes(query: str):
        words = [w.lower() for w in query.split() if len(w) > 2] or [query.lower()]
        hits = []
        for p in sorted(folder.glob("*.md"), reverse=True):
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(w in line.lower() for w in words):
                    hits.append(f"{p.stem}: {line.strip()[:200]}")
            if len(hits) >= 25:
                break
        return hits or "No matching notes."

    @reg.tool("list_notes", "List the most recent note files.", {}, group="notes", read_only=True)
    def list_notes():
        return [p.stem for p in sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]] \
            or "No notes yet."

    @reg.tool("read_note", "Read a note file by name (from list_notes).", {"name": {"type": "string"}},
              group="notes", read_only=True)
    def read_note(name: str):
        p = folder / f"{_safe(name)}.md"
        return p.read_text(encoding="utf-8")[:5000] if p.exists() else f"ERROR: no note called {name}."

    return folder
