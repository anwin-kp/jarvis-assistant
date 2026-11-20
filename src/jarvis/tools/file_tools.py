"""File tools, sandboxed to the folders listed in config.security.allowed_roots."""

import os
import shutil
from pathlib import Path

from send2trash import send2trash

from jarvis.tools.registry import Risk, ToolRegistry

KNOWN_FOLDERS = {
    "documents": "FOLDERID_Documents",
    "desktop": "FOLDERID_Desktop",
    "downloads": "FOLDERID_Downloads",
    "pictures": "FOLDERID_Pictures",
    "music": "FOLDERID_Music",
    "videos": "FOLDERID_Videos",
}


def resolve_root(entry: str) -> Path:
    """Turn 'documents' etc. into the real (possibly OneDrive-redirected) folder."""
    key = entry.lower().strip()
    if key in KNOWN_FOLDERS:
        try:
            from win32com.shell import shell, shellcon

            return Path(shell.SHGetKnownFolderPath(getattr(shellcon, KNOWN_FOLDERS[key]), 0, None))
        except Exception:
            return Path.home() / key.capitalize()
    return Path(entry).expanduser()


def make_guard(allowed_roots: list[str | Path]):
    roots = [Path(r).resolve() for r in allowed_roots]

    def guard(path: str) -> Path:
        p = Path(path).expanduser().resolve()  # resolves '..' and symlinks
        if not any(p == r or p.is_relative_to(r) for r in roots):
            raise PermissionError(f"{p} is outside the folders I'm allowed to use")
        return p

    return guard


def register(reg: ToolRegistry, allowed_roots: list[str]) -> list[Path]:
    roots = [resolve_root(r) for r in allowed_roots]
    roots = [r for r in roots if r.exists()]
    guard = make_guard(roots)
    roots_text = "; ".join(str(r) for r in roots)

    @reg.tool(
        "list_allowed_folders",
        "List the folders I can access, with their full paths.",
        {},
        group="files",
        read_only=True,
    )
    def list_allowed_folders():
        return [str(r) for r in roots]

    @reg.tool(
        "search_files",
        f"Find files or folders whose name contains the query. Allowed folders: {roots_text}",
        {
            "query": {"type": "string", "description": "Part of the file name, e.g. 'resume' or '.pdf'"},
            "folder": {"type": "string", "description": "Full path of an allowed folder to search in"},
        },
        group="files",
        read_only=True,
    )
    def search_files(query: str, folder: str):
        base, q, hits = guard(folder), query.lower(), []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
            for n in dirnames + filenames:
                if q in n.lower():
                    hits.append(str(Path(dirpath) / n))
                    if len(hits) >= 20:
                        return hits
        return hits or "No matches."

    @reg.tool(
        "list_folder",
        "List the contents of a folder.",
        {"folder": {"type": "string"}},
        group="files",
        read_only=True,
    )
    def list_folder(folder: str):
        base = guard(folder)
        items = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        out = [f"{'[dir] ' if p.is_dir() else ''}{p.name}" for p in items[:60]]
        if len(items) > 60:
            out.append(f"... and {len(items) - 60} more")
        return out or "The folder is empty."

    @reg.tool(
        "open_file",
        "Open a file or folder with its default application.",
        {"path": {"type": "string"}},
        group="files",
    )
    def open_file(path: str):
        p = guard(path)
        if not p.exists():
            return f"ERROR: {p} does not exist."
        os.startfile(p)
        return f"Opened {p.name}."

    @reg.tool(
        "create_folder",
        "Create a new folder.",
        {"path": {"type": "string"}},
        risk=Risk.CONFIRM,
        group="files",
    )
    def create_folder(path: str):
        p = guard(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"Created {p}."

    @reg.tool(
        "move_file",
        "Move or rename a file or folder. Never overwrites an existing file.",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        risk=Risk.CONFIRM,
        group="files",
    )
    def move_file(source: str, destination: str):
        src, dst = guard(source), guard(destination)
        if not src.exists():
            return f"ERROR: {src} does not exist."
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            return f"ERROR: {dst} already exists; I won't overwrite it."
        shutil.move(str(src), str(dst))
        return f"Moved to {dst}."

    @reg.tool(
        "delete_file",
        "Move a file or folder to the Recycle Bin.",
        {"path": {"type": "string"}},
        risk=Risk.DANGER,
        group="files",
    )
    def delete_file(path: str):
        p = guard(path)
        if p in [r.resolve() for r in roots]:
            raise PermissionError("I won't delete a whole allowed folder")
        if not p.exists():
            return f"ERROR: {p} does not exist."
        send2trash(str(p))
        return f"Moved {p.name} to the Recycle Bin."

    return roots
