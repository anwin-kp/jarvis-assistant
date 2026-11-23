"""Coding, part A+B: know your projects, read and search their code, git status/diff, and run each project's own
dev commands (tests, analyze, build) from an allowlist. Edits are done by the coding agent (agent/coder.py) on its
own git branch, never here.

Safety: only folders that are projects (a .git or a known project file), never secret files (.env, keys, tokens,
signing keystores), commands only from the project's detected allowlist (no shell, fixed argument lists)."""

import difflib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.tools.registry import ToolRegistry

SKIP_DIRS = {".git", "node_modules", "build", ".dart_tool", ".venv", "venv", "__pycache__", ".idea", ".gradle",
             "dist", "bin", "obj", "target", ".pytest_cache", ".ruff_cache", ".next", "Pods", ".vs"}
# secret files, by NAME only (a whole-path match refused calc.py inside a folder called "...secrets...")
SECRET = re.compile(r"^(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks|keystore)|credentials.*\.json|token.*\.json|id_rsa.*|"
                    r"key\.properties|local\.properties|google-services\.json|GoogleService-Info\.plist|"
                    r".*secret.*)$", re.I)


def is_secret(path) -> bool:
    return bool(SECRET.match(Path(str(path)).name))


SYSTEM_DIRS = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "recovery",
               "system volume information", "perflogs", "documents and settings", "users", "onedrivetemp",
               "ahcache", "system.sav", "intel", "amd", "nvidia", "hp", "dell", "inetpub", "riot games", "xboxgames"}
MAX_OUT = 3000


@dataclass
class Project:
    name: str
    path: Path
    kinds: list[str] = field(default_factory=list)


def kinds_of(path: Path) -> list[str]:
    k = []
    if (path / "pubspec.yaml").exists():
        k.append("flutter")
    if (path / "pyproject.toml").exists() or (path / "setup.py").exists() or (path / "requirements.txt").exists():
        k.append("python")
    if (path / "package.json").exists():
        k.append("node")
    if any(path.glob("*.sln")) or any(path.glob("*.csproj")):
        k.append("dotnet")
    if (path / "go.mod").exists():
        k.append("go")
    if (path / "Cargo.toml").exists():
        k.append("rust")
    return k


def discover(roots: list[Path], depth: int = 2) -> dict[str, Project]:
    """Git repositories (and project folders) under the roots, by folder name."""
    found: dict[str, Project] = {}

    def walk(d: Path, level: int):
        try:
            if (d / ".git").exists() or kinds_of(d):
                name = d.name.lower()
                if name not in found:
                    found[name] = Project(name, d, kinds_of(d))
                return
            if level >= depth:
                return
            for c in d.iterdir():
                if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS:
                    walk(c, level + 1)
        except OSError:
            pass

    for r in roots:
        if r.is_dir():
            walk(r, 0)
    return found


def default_roots() -> list[Path]:
    """Where developers keep code: user folders + top-level folders of the system drive (C:\\Dev, C:\\Projects)."""
    from jarvis.tools.file_tools import resolve_root

    home = Path.home()
    roots = [home / d for d in ("source/repos", "projects", "Projects", "dev", "Developer", "code", "src", "git",
                                "repos", "StudioProjects", "AndroidStudioProjects")]
    roots += [resolve_root("documents"), resolve_root("desktop")]
    drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
    try:
        roots += [d for d in drive.iterdir() if d.is_dir() and d.name.lower() not in SYSTEM_DIRS
                  and not d.name.startswith(("$", "."))]
    except OSError:
        pass
    return roots


def _git(path: Path, *args: str, timeout: int = 20) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    return (r.stdout or r.stderr).strip("\r\n")  # not strip(): "git status --short" starts with a space


def dev_commands(project: Project, extra: list[str] | None = None) -> dict[str, list[str]]:
    """The commands this project allows: name -> argv (no shell). Detected from the project's own files."""
    p, cmds = project.path, {}
    if "flutter" in project.kinds:
        cmds |= {"test": ["flutter", "test"], "analyze": ["flutter", "analyze"], "pub get": ["flutter", "pub", "get"],
                 "build apk": ["flutter", "build", "apk"], "format check": ["dart", "format", "--output=none",
                                                                             "--set-exit-if-changed", "."]}
    if "python" in project.kinds:
        py = p / ".venv" / "Scripts" / "python.exe"
        py = str(py) if py.exists() else "python"
        cmds |= {"test": [py, "-m", "pytest", "-q"], "lint": [py, "-m", "ruff", "check", "."]}
    if "node" in project.kinds:
        import json

        try:
            scripts = json.loads((p / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        for s in scripts:
            if s in ("test", "lint", "build", "typecheck", "check", "format:check"):
                cmds["test" if s == "test" and "test" not in cmds else s] = ["npm", "run", s]
        cmds.setdefault("install", ["npm", "install"])
    if "dotnet" in project.kinds:
        cmds |= {"build": ["dotnet", "build"], "test": ["dotnet", "test"]}
    if "go" in project.kinds:
        cmds |= {"test": ["go", "test", "./..."], "build": ["go", "build", "./..."]}
    if "rust" in project.kinds:
        cmds |= {"test": ["cargo", "test"], "build": ["cargo", "build"]}
    for c in extra or []:  # config coding.commands: {project: ["npm run e2e"]}
        cmds[c] = c.split()
    return cmds


CHANGES = re.compile(r"\b(get|install|build)\b|^format$")  # commands that change files: ask first
RESULT_LINES = (  # how each tool reports its result
    r"\d+ (?:passed|failed)(?:, \d+ \w+)*(?: in [\d.]+s)?",  # pytest: "1 failed, 2 passed in 0.12s"
    r"All tests passed!|Some tests failed\.",  # flutter test
    r"No issues found!|\d+ issues? found\.?",  # flutter analyze
    r"(?:Passed|Failed)!\s+- Failed:\s+\d+, Passed:\s+\d+.*",  # dotnet test
    r"Tests?:\s+\d+ (?:passed|failed).*",  # jest
    r"Build succeeded\.|Build FAILED\.|BUILD SUCCESSFUL|BUILD FAILED",
    r"Found \d+ errors?\.|All checks passed!",  # ruff
)


def summarize_output(name: str, code: int, out: str) -> str:
    """The result in a line: test counts, issue counts, or the first errors."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    found = []
    for pattern in RESULT_LINES:
        if m := re.search(pattern, out):
            found.append(m.group(0).strip())
    errors = [ln.strip() for ln in lines if re.search(r"\b(error|failed|FAIL|Exception|✗)\b", ln)][:8]
    head = f"{name}: {'OK' if code == 0 else f'FAILED (exit {code})'}"
    text = head + (f" - {'; '.join(found)}" if found else "")
    if code != 0 and errors:
        text += "\nFirst errors:\n" + "\n".join(errors)
    elif not found:
        text += "\nLast output:\n" + "\n".join(lines[-12:])
    return text[:MAX_OUT]


def register(reg: ToolRegistry, cfg: dict) -> None:
    ccfg = cfg.get("coding") or {}
    state = {"projects": None, "at": 0.0}

    def projects() -> dict[str, Project]:
        if state["projects"] is None or time.monotonic() - state["at"] > 300:  # re-scan every 5 minutes
            roots = [Path(r).expanduser() for r in ccfg.get("search_roots") or []] or default_roots()
            found = discover(roots)
            for name, path in (ccfg.get("projects") or {}).items():  # named in config: always included
                p = Path(path).expanduser()
                if p.is_dir():
                    found[name.lower()] = Project(name.lower(), p, kinds_of(p))
            state["projects"], state["at"] = found, time.monotonic()
        return state["projects"]

    def find_project(name: str) -> Project:
        all_ = projects()
        n = name.lower().strip().removesuffix(" project").removesuffix(" app").strip()
        if n in all_:
            return all_[n]
        close = difflib.get_close_matches(n, list(all_), n=1, cutoff=0.6)
        part = [k for k in all_ if n and (n in k or k in n)]
        hit = close[0] if close else (part[0] if len(part) == 1 else None)
        if not hit:
            raise LookupError(f"no project called '{name}'. Projects: {', '.join(sorted(all_)) or 'none found'}")
        return all_[hit]

    def inside(pr: Project, rel: str) -> Path:
        p = (pr.path / rel).resolve()
        if not (p == pr.path.resolve() or p.is_relative_to(pr.path.resolve())):
            raise PermissionError(f"{rel} is outside the project")
        if is_secret(p):
            raise PermissionError(f"{p.name} looks like a secret (keys, tokens, .env): I don't read those")
        return p

    reg.code_projects = projects  # the coding agent and tests use the same view
    reg.code_project = find_project
    names = sorted(projects())
    known = f" The user's projects: {', '.join(names)}." if names else ""
    if names:  # "where is login in ark sales": the model must know 'ark sales' is a project, not something on screen
        from jarvis.llm.tool_groups import add_keywords

        add_keywords("code", names + [n.replace("_", " ").replace("-", " ") for n in names])

    @reg.tool("list_projects", "List the user's code projects (git repos) with their type (flutter, python, node, "
              "dotnet...), path and current branch." + known, {}, group="code", read_only=True)
    def list_projects():
        rows = []
        for p in sorted(projects().values(), key=lambda p: p.name):
            branch = _git(p.path, "branch", "--show-current") if (p.path / ".git").exists() else ""
            rows.append(f"{p.name} ({', '.join(p.kinds) or 'code'}){f' [{branch}]' if branch else ''}: {p.path}")
        return rows or "No projects found. Add them to config.yaml: coding.projects: {name: path}."

    @reg.tool("open_project", "Open a code project in VS Code (or File Explorer with app='explorer').",
              {"project": {"type": "string"}, "app": {"type": "string", "enum": ["vscode", "explorer"]}},
              required=["project"], group="code")
    def open_project(project: str, app: str = "vscode"):
        pr = find_project(project)
        if app == "explorer":
            os.startfile(pr.path)
            return f"Opened {pr.name} in File Explorer."
        code = shutil.which("code")
        exe = Path(code).parent.parent / "Code.exe" if code else None  # ...\Microsoft VS Code\bin\code.cmd
        if not exe or not exe.exists():
            return "ERROR: VS Code isn't installed (or 'code' isn't on PATH)."
        subprocess.Popen([str(exe), str(pr.path)], creationflags=subprocess.CREATE_NO_WINDOW)
        return f"Opened {pr.name} in VS Code ({pr.path})."

    @reg.tool("project_status", "A code project's git state: branch, changed files, last commits.",
              {"project": {"type": "string"}}, group="code", read_only=True)
    def project_status(project: str):
        pr = find_project(project)
        if not (pr.path / ".git").exists():
            return f"{pr.name} is not a git repository."
        changed = _git(pr.path, "status", "--short")
        return (f"{pr.name} on branch {_git(pr.path, 'branch', '--show-current') or '(detached)'}\n"
                f"Changed files:\n{changed or '(none: clean)'}\n"
                f"Last commits:\n{_git(pr.path, 'log', '--oneline', '-8')}")[:MAX_OUT]

    @reg.tool("git_diff", "Show what changed in a project (uncommitted changes, or against a branch/commit).",
              {"project": {"type": "string"}, "against": {"type": "string", "description": "branch or commit"}},
              required=["project"], group="code", read_only=True)
    def git_diff(project: str, against: str = ""):
        pr = find_project(project)
        args = ["diff", "--stat"] + ([against] if against else [])
        if against and not re.fullmatch(r"[\w./@^~-]{1,80}", against):
            return "ERROR: not a branch or commit name."
        stat = _git(pr.path, *args)
        full = _git(pr.path, *(["diff"] + ([against] if against else [])))
        return (f"{stat or 'No changes.'}\n\n{full}")[:MAX_OUT]

    @reg.tool("search_code", "Search a project's code for text (where is X handled / defined / used); returns "
              "file:line: text." + known,
              {"project": {"type": "string"}, "query": {"type": "string"},
               "file_glob": {"type": "string", "description": "e.g. *.dart (optional)"}},
              required=["project", "query"], group="code", read_only=True)
    def search_code(project: str, query: str, file_glob: str = ""):
        pr = find_project(project)
        if (pr.path / ".git").exists():
            args = ["grep", "-n", "-I", "-i", "--full-name", "-e", query, "--"] + ([file_glob] if file_glob else [])
            out = [ln for ln in _git(pr.path, *args).splitlines() if not is_secret(ln.split(":", 1)[0])]
        else:
            out = []
            rx = re.compile(re.escape(query), re.I)
            for f in iter_files(pr.path, file_glob):
                try:
                    for i, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(ln):
                            out.append(f"{f.relative_to(pr.path)}:{i}:{ln}")
                except OSError:
                    continue
                if len(out) > 60:
                    break
        return "\n".join(ln[:200] for ln in out[:40]) or f"No matches for '{query}'."

    @reg.tool("read_code_file", "Read part of a file in a project (path relative to the project).",
              {"project": {"type": "string"}, "path": {"type": "string"},
               "start_line": {"type": "integer"}, "lines": {"type": "integer"}},
              required=["project", "path"], group="code", read_only=True)
    def read_code_file(project: str, path: str, start_line: int = 1, lines: int = 150):
        pr = find_project(project)
        f = inside(pr, path)
        if not f.is_file():
            return f"ERROR: {path} doesn't exist in {pr.name}."
        all_lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, int(start_line or 1))
        chunk = all_lines[s - 1:s - 1 + max(1, min(int(lines or 150), 400))]
        body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(chunk, s))
        return f"{path} (lines {s}-{s + len(chunk) - 1} of {len(all_lines)})\n{body}"[:6000]

    @reg.tool("list_code_files", "List files in a project (tracked by git), optionally under a folder.",
              {"project": {"type": "string"}, "folder": {"type": "string"}}, required=["project"], group="code",
              read_only=True)
    def list_code_files(project: str, folder: str = ""):
        pr = find_project(project)
        if (pr.path / ".git").exists():
            files = _git(pr.path, "ls-files", "--", folder or ".").splitlines()
        else:
            files = [str(f.relative_to(pr.path)) for f in iter_files(inside(pr, folder or "."))]
        files = [f for f in files if not is_secret(f)]
        return "\n".join(files[:200]) + (f"\n... and {len(files) - 200} more" if len(files) > 200 else "")

    @reg.tool("list_dev_commands", "The dev commands JARVIS may run in a project (test, analyze, build...).",
              {"project": {"type": "string"}}, group="code", read_only=True)
    def list_dev_commands(project: str):
        pr = find_project(project)
        cmds = dev_commands(pr, (ccfg.get("commands") or {}).get(pr.name))
        return {k: " ".join(v) for k, v in cmds.items()} or f"No known commands for {pr.name}."

    @reg.tool("run_dev_command", "Run one of a project's dev commands (see list_dev_commands): 'test', 'analyze', "
              "'lint', 'build apk'... Returns pass/fail with test or issue counts and the first errors.",
              {"project": {"type": "string"}, "command": {"type": "string"}}, group="code")
    def run_dev_command(project: str, command: str):
        pr = find_project(project)
        cmds = dev_commands(pr, (ccfg.get("commands") or {}).get(pr.name))
        key = command.lower().strip()
        key = next((k for k in cmds if k == key or " ".join(cmds[k]) == key), None) or \
            next((k for k in cmds if key in k), None)
        if not key:
            return f"ERROR: '{command}' isn't allowed in {pr.name}. Allowed: {', '.join(cmds) or 'none'}."
        if CHANGES.search(key) and not reg.ask(f"run '{' '.join(cmds[key])}' in {pr.name} (it changes files)"):
            return "Not run: you didn't approve it."
        return run_command(pr.path, key, cmds[key], int(ccfg.get("command_timeout", 900)))


def iter_files(base: Path, glob: str = ""):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for n in filenames:
            if (not glob or Path(n).match(glob)) and not is_secret(n):
                yield Path(dirpath) / n


def run_command(cwd: Path, name: str, argv: list[str], timeout: int = 900, env: dict | None = None) -> str:
    exe = shutil.which(argv[0]) or argv[0]  # flutter.bat / npm.cmd: the full path (no shell involved)
    t = time.perf_counter()
    try:
        r = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
                           env={**os.environ, **(env or {})})
    except FileNotFoundError:
        return f"ERROR: '{argv[0]}' isn't installed (or not on PATH)."
    except subprocess.TimeoutExpired:
        return f"ERROR: '{name}' took longer than {timeout // 60} minutes; stopped."
    took = time.perf_counter() - t
    return summarize_output(name, r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")) + f"\n({took:.0f} s)"
