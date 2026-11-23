"""The coding agent (coding part C): "Jarvis, fix the failing test in modbus codec".

It works in its own git worktree on a new branch (jarvis/<task>), so your checkout is never touched - not even with
uncommitted changes. A free cloud model (Groq gpt-oss-120b first, see coding.agent_models) gets a few narrow tools:
list / read / search files, exact find-and-replace edits, create files, the project's allowlisted checks (tests,
analyze, lint) and finish. Never secrets, never outside the worktree, no shell. It runs in the background; when it
finishes JARVIS says what changed and the test result, and you merge or discard the branch.

Context is kept small (old tool output is trimmed): Groq's free tier allows 8k tokens per minute per model."""

import datetime as dt
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

from jarvis.tools.code_tools import Project, dev_commands, is_secret, run_command

SYSTEM = """You are a careful senior software engineer working on the user's project in a git working copy.
Task: {task}
Project: {name} ({kinds}). Allowed checks: {checks}.
Work like this:
1. Find the relevant code (search, list_files, read_file with line ranges; read only what you need).
2. Make the smallest correct change with replace_in_file (exact text, unique match) or write_file (new files).
3. Run a check (run_check) when one exists, and fix what it reports.
4. Call finish with a 1-3 sentence summary of what you changed and the check result.
Rules: keep the project's style; never touch secrets or generated/build files; don't reformat unrelated code;
if the task is unclear or impossible, call finish and say why. Be brief: no commentary between tool calls."""

TOOLS = [
    {"name": "list_files", "description": "List files in the project (optionally under a folder).",
     "parameters": {"type": "object", "properties": {"folder": {"type": "string"}}, "required": []}},
    {"name": "read_file", "description": "Read lines of a file (numbered).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                                     "lines": {"type": "integer"}}, "required": ["path"]}},
    {"name": "search", "description": "Find text in the project's files (case-insensitive). Returns file:line: text.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "replace_in_file", "description": "Replace an exact, unique piece of text in a file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                     "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "write_file", "description": "Create a new file (or fully rewrite a small one).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "run_check", "description": "Run one of the allowed checks (e.g. test, analyze, lint).",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "finish", "description": "Done (or can't be done): summary of what changed and the check result.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]
CHECKS = ("test", "analyze", "lint", "typecheck", "check", "format check")


@dataclass
class CodeTask:
    id: str
    project: str
    path: str
    task: str
    branch: str
    base: str
    status: str = "running"  # running / done / failed / cancelled / merged / discarded
    summary: str = ""
    files: list[str] = field(default_factory=list)
    check: str = ""
    model: str = ""
    steps: int = 0
    started: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))


def git(path, *args, check=False, timeout=60) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return (r.stdout or r.stderr).strip("\r\n")


def slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))[:32].strip("-") or "task"


class Workspace:
    """The agent's file tools, confined to one worktree."""

    def __init__(self, root: Path, project: Project, extra_cmds=None):
        self.root, self.project = root.resolve(), project
        allowed = set(CHECKS) | set(extra_cmds or [])  # checks only: never build/install from the agent
        self.cmds = {k: v for k, v in dev_commands(project, extra_cmds).items() if k in allowed}
        self.changed: set[str] = set()

    def _path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root) or ".git" in p.relative_to(self.root).parts:
            raise PermissionError(f"{rel}: outside the project")
        if is_secret(p):
            raise PermissionError(f"{rel}: secret files are off limits")
        return p

    def list_files(self, folder: str = "") -> str:
        files = [f for f in git(self.root, "ls-files", "--", folder or ".").splitlines() if not is_secret(f)]
        return "\n".join(files[:300]) + (f"\n... {len(files) - 300} more" if len(files) > 300 else "")

    def read_file(self, path: str, start_line: int = 1, lines: int = 200) -> str:
        f = self._path(path)
        if not f.is_file():
            return f"ERROR: {path} doesn't exist."
        all_ = f.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, int(start_line or 1))
        chunk = all_[s - 1:s - 1 + max(1, min(int(lines or 200), 300))]
        return f"{path} lines {s}-{s + len(chunk) - 1} of {len(all_)}\n" + \
            "\n".join(f"{i}: {ln}" for i, ln in enumerate(chunk, s))

    def search(self, query: str) -> str:
        out = git(self.root, "grep", "-n", "-I", "-i", "-F", "-e", query)
        rows = [ln[:200] for ln in out.splitlines() if not is_secret(ln.split(":", 1)[0])]
        return "\n".join(rows[:40]) or "No matches."

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        f = self._path(path)
        if not f.is_file():
            return f"ERROR: {path} doesn't exist (use write_file for new files)."
        text = f.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            return (f"ERROR: the old text appears {n} times in {path}; it must match exactly once "
                    "(include more surrounding lines).")
        f.write_text(text.replace(old, new), encoding="utf-8", newline="")
        self.changed.add(path)
        return f"Replaced in {path}."

    def write_file(self, path: str, content: str) -> str:
        f = self._path(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8", newline="")
        self.changed.add(path)
        return f"Wrote {path} ({len(content.splitlines())} lines)."

    def run_check(self, name: str) -> str:
        key = next((k for k in self.cmds if k == name.lower().strip()), None)
        if not key:
            return f"ERROR: allowed checks: {', '.join(self.cmds) or 'none'}."
        env = {"PYTHONPATH": f"{self.root / 'src'};{self.root}"}  # test this copy, not an installed one
        return run_command(self.root, key, self.cmds[key], 600, env=env)


def compact(messages: list[dict], keep: int = 4, limit: int = 300) -> list[dict]:
    """Trim old tool results (keep the newest `keep` whole): every step resends everything, and Groq's free tier
    allows 8k tokens/minute."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = set(tool_idx[:-keep]) if keep else set(tool_idx)
    return [{**m, "content": m["content"][:limit] + " …(trimmed)"} if i in old and len(m["content"]) > limit else m
            for i, m in enumerate(messages)]


class Coder:
    def __init__(self, models: list[dict], data: Path, notify=None, max_steps: int = 30, max_minutes: float = 15):
        self.models, self.data = models, data
        self.notify = notify or (lambda text: logger.info(text))
        self.max_steps, self.max_minutes = max_steps, max_minutes
        self.file = data / "code-tasks.json"
        self.cancel = threading.Event()
        self.current: CodeTask | None = None

    # ---------------------------------------------------------------- task records
    def tasks(self) -> list[CodeTask]:
        try:
            return [CodeTask(**t) for t in json.loads(self.file.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return []

    def save(self, task: CodeTask) -> None:
        rows = [t for t in self.tasks() if t.id != task.id] + [task]
        self.data.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps([asdict(t) for t in rows[-30:]], indent=1), encoding="utf-8")

    def find(self, task_id: str = "") -> CodeTask | None:
        rows = self.tasks()
        if task_id:
            return next((t for t in rows if task_id in (t.id, t.branch)), None)
        return rows[-1] if rows else None

    # ---------------------------------------------------------------- start
    def start(self, project: Project, task_text: str, extra_cmds=None, background: bool = True) -> CodeTask:
        if self.current and self.current.status == "running":
            raise RuntimeError(f"already working on '{self.current.task}' in {self.current.project}")
        if not (project.path / ".git").exists():
            raise RuntimeError(f"{project.name} isn't a git repository (the agent works on its own branch)")
        base = git(project.path, "branch", "--show-current") or git(project.path, "rev-parse", "--short", "HEAD")
        tid = dt.datetime.now().strftime("%m%d-%H%M%S")
        branch = f"jarvis/{slug(task_text)}-{tid}"
        task = CodeTask(tid, project.name, str(project.path), task_text, branch, base)
        self.current, self.cancel = task, threading.Event()
        self.save(task)
        args = (task, project, extra_cmds)
        if background:
            threading.Thread(target=self._run, args=args, daemon=True, name="coder").start()
        else:
            self._run(*args)
        return task

    def _run(self, task: CodeTask, project: Project, extra_cmds) -> None:
        wt = self.data / "code-worktrees" / task.id
        try:
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(project.path, "worktree", "add", "-q", "-b", task.branch, str(wt), "HEAD", check=True)
            for dep in ("node_modules",):  # dependencies aren't in git: link them so checks can run
                if (project.path / dep).is_dir() and not (wt / dep).exists():
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(wt / dep), str(project.path / dep)],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            # the original project's paths for its tools (python checks use its own .venv), the worktree as cwd
            ws = Workspace(wt, Project(project.name, project.path, project.kinds), extra_cmds)
            summary = self._loop(task, ws, project)
            task.files = sorted(ws.changed)
            if ws.changed:
                git(wt, "add", "--", *sorted(ws.changed))  # only what it edited: checks leave caches (*.pyc...)
                git(wt, "-c", "user.name=JARVIS", "-c", "user.email=jarvis@localhost", "commit", "-q", "-m",
                    f"jarvis: {task.task[:60]}\n\n{summary}", check=True)
                if check := next((k for k in ("test", "analyze", "lint") if k in ws.cmds), None):
                    task.check = ws.run_check(check).splitlines()[0]
            task.summary = summary
            task.status = "cancelled" if self.cancel.is_set() else ("done" if ws.changed else "failed")
        except Exception as e:
            logger.exception("Code task failed")
            task.status, task.summary = "failed", f"{type(e).__name__}: {e}"
        finally:
            if wt.exists():
                git(project.path, "worktree", "remove", "--force", str(wt))
            if task.status != "done":  # nothing worth keeping on the branch
                git(project.path, "branch", "-D", task.branch)
            self.save(task)
            self.notify(self.report(task))

    def report(self, t: CodeTask) -> str:
        if t.status == "done":
            return (f"Code task done in {t.project}: {t.summary} Changed: {', '.join(t.files[:6])}."
                    + (f" Check: {t.check}." if t.check else "")
                    + f" It's on branch {t.branch}: say 'show the changes', 'merge it' or 'discard it'.")
        return f"Code task in {t.project} {t.status}: {t.summary}"

    # ---------------------------------------------------------------- the agent loop
    def _loop(self, task: CodeTask, ws: Workspace, project: Project) -> str:
        from jarvis.llm.cloud_brain import CloudBrain, CloudUnavailable

        brains = [CloudBrain(None, m["provider"], m["model"], timeout=m.get("timeout", 60),
                             extra={k: v for k, v in m.items() if k not in ("provider", "model", "timeout")})
                  for m in self.models]
        brains = [b for b in brains if b.available]
        if not brains:
            raise RuntimeError("no cloud model key set (set-key groq): the coding agent needs a free cloud model")
        tools = [{"type": "function", "function": t} for t in TOOLS]
        msgs = [{"role": "system", "content": SYSTEM.format(task=task.task, name=project.name,
                                                            kinds=", ".join(project.kinds) or "code",
                                                            checks=", ".join(ws.cmds) or "none")},
                {"role": "user", "content": f"Start. Top-level files:\n{ws.list_files()[:1500]}"}]
        cooldown: dict[str, float] = {}
        deadline = time.monotonic() + self.max_minutes * 60
        for step in range(self.max_steps):
            task.steps = step + 1
            if self.cancel.is_set():
                return "Cancelled by the user."
            if time.monotonic() > deadline:
                return f"Stopped after {self.max_minutes:g} minutes (time limit)."
            msg = None
            for b in brains:
                wait = cooldown.get(b.name, 0) - time.monotonic()
                if wait > 0:
                    continue
                try:
                    msg = b._chat(compact(msgs), tools)
                    task.model = b.name
                    break
                except CloudUnavailable as e:
                    logger.info("Coder: {} unavailable ({}); next model", b.name, e)
                    cooldown[b.name] = time.monotonic() + max(getattr(e, "retry_after", 30), 5)
            if msg is None:  # every model rate limited: wait for the first to come back
                time.sleep(min(30, max(1.0, min(cooldown.values()) - time.monotonic())))
                continue
            calls = msg.get("tool_calls") or []
            msgs.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls
                                                                                       else {})})
            if not calls:
                msgs.append({"role": "user", "content": "Use the tools; call finish when done."})
                continue
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                if name == "finish":
                    return str(args.get("summary") or "Done.").strip()
                try:
                    fn = getattr(ws, name) if name in {t["name"] for t in TOOLS} else None
                    if fn:  # models invent arguments ("line_end"): keep only the declared ones
                        import inspect

                        args = {k: v for k, v in args.items() if k in inspect.signature(fn).parameters}
                    out = fn(**args) if fn else f"ERROR: unknown tool {name}"
                except Exception as e:  # bad arguments, permission: tell the model
                    out = f"ERROR: {e}"
                logger.debug("Coder step {} {} -> {}", step, name, str(out)[:120])
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or f"call_{step}", "content": str(out)[:4000]})
        return f"Stopped after {self.max_steps} steps (step limit)."

    # ---------------------------------------------------------------- after: look, merge, discard
    def changes(self, task: CodeTask) -> str:
        stat = git(task.path, "diff", "--stat", f"{task.base}...{task.branch}")
        diff = git(task.path, "diff", f"{task.base}...{task.branch}")
        return f"{task.branch} vs {task.base}\n{stat}\n\n{diff}"[:3500]

    def merge(self, task: CodeTask) -> str:
        cur = git(task.path, "branch", "--show-current")
        if cur != task.base:
            return f"ERROR: {task.project} is on '{cur}', not '{task.base}': switch back first, then merge."
        if git(task.path, "status", "--porcelain", "--untracked-files=no"):
            return f"ERROR: {task.project} has uncommitted changes: commit or stash them first."
        r = subprocess.run(["git", "-C", task.path, "merge", "--no-ff", "-m",
                            f"Merge {task.branch} (JARVIS: {task.task[:50]})", task.branch],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        out = (r.stdout or "") + (r.stderr or "")
        if "CONFLICT" in out or "Automatic merge failed" in out:
            git(task.path, "merge", "--abort")
            return f"ERROR: the merge has conflicts, so I undid it. Merge {task.branch} yourself."
        # checked, never assumed: a failed merge (no git identity, a lock...) used to be reported as merged
        merged = subprocess.run(["git", "-C", task.path, "merge-base", "--is-ancestor", task.branch, "HEAD"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
        if r.returncode or not merged:
            return f"ERROR: the merge didn't happen: {out.strip()[:200]}"
        git(task.path, "branch", "-d", task.branch)
        task.status = "merged"
        self.save(task)
        return f"Merged {task.branch} into {task.base} (checked: {git(task.path, 'log', '--oneline', '-1')})."

    def discard(self, task: CodeTask) -> str:
        if task.status == "running":
            self.cancel.set()
            return "Cancelling the running task; its branch will be removed."
        git(task.path, "branch", "-D", task.branch)
        task.status = "discarded"
        self.save(task)
        return f"Discarded {task.branch}."


def register(reg, cfg: dict, data: Path) -> Coder:
    ccfg = cfg.get("coding") or {}
    models = ccfg.get("agent_models") or [{"provider": "groq", "model": "openai/gpt-oss-120b"}]
    coder = Coder(models, data, notify=lambda text: (getattr(reg, "notify", None) or logger.info)(text))
    cleanup(data)
    for t in coder.tasks():  # JARVIS restarted mid-task: that task is over
        if t.status == "running":
            t.status, t.summary = "failed", "interrupted (JARVIS restarted)"
            git(t.path, "worktree", "prune")
            coder.save(t)
    from jarvis.tools.registry import Risk

    def pick(task_id: str) -> CodeTask:
        t = coder.find(task_id)
        if not t:
            raise LookupError("no code task yet")
        return t

    @reg.tool("code_task", "Have the coding agent CHANGE code in a project: fix a bug or failing test, add a feature, "
              "refactor. It works in the background on its own git branch (your files are untouched) with a free "
              "cloud model, runs the project's checks, and reports back; then merge or discard.",
              {"project": {"type": "string"}, "task": {"type": "string", "description": "what to change, in detail"}},
              risk=Risk.CONFIRM, group="code")
    def code_task(project: str, task: str):
        pr = reg.code_project(project)
        t = coder.start(pr, task, (ccfg.get("commands") or {}).get(pr.name))
        return (f"Started: working on '{task}' in {pr.name}, on branch {t.branch}. Your files stay as they are. "
                "I'll tell you when it's done (usually 1-5 minutes).")

    @reg.tool("code_task_status", "The coding agent's current or last task: status, summary, files, check result.",
              {}, group="code", read_only=True)
    def code_task_status():
        rows = coder.tasks()[-5:]
        if not rows:
            return "No code tasks yet."
        return "\n".join(f"{t.id} [{t.status}] {t.project}: {t.task[:60]} -> {t.summary[:150]} "
                         f"{'(step ' + str(t.steps) + ')' if t.status == 'running' else ''}" for t in reversed(rows))

    @reg.tool("code_task_changes", "Show the diff of the coding agent's last (or given) task.",
              {"task_id": {"type": "string"}}, required=[], group="code", read_only=True)
    def code_task_changes(task_id: str = ""):
        t = pick(task_id)
        return coder.changes(t) if t.status == "done" else f"Task {t.id} is {t.status}: {t.summary}"

    @reg.tool("merge_code_task", "Merge the coding agent's branch into the branch it started from.",
              {"task_id": {"type": "string"}}, required=[], risk=Risk.CONFIRM, group="code")
    def merge_code_task(task_id: str = ""):
        t = pick(task_id)
        return coder.merge(t) if t.status == "done" else f"ERROR: task {t.id} is {t.status}, nothing to merge."

    @reg.tool("discard_code_task", "Throw away the coding agent's branch (or cancel the running task).",
              {"task_id": {"type": "string"}}, required=[], risk=Risk.CONFIRM, group="code")
    def discard_code_task(task_id: str = ""):
        return coder.discard(pick(task_id))

    return coder


def cleanup(data: Path) -> None:
    """Worktrees left by a crash: remove the folders (git prunes their records on the next worktree command)."""
    folder = data / "code-worktrees"
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
