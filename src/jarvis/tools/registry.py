"""Tool registry and permission gate.

The model can only *request* tools. This module decides whether a request runs,
needs the user's approval, or is refused. Permissions are enforced here in code,
never by the prompt.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger


class Risk(Enum):
    SAFE = "safe"  # read-only or trivially reversible: runs automatically
    CONFIRM = "confirm"  # changes something: quick yes/no
    DANGER = "danger"  # destructive or outward-facing: explicit approval


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    risk: Risk
    group: str
    func: Callable[..., Any]
    read_only: bool = False  # True = never changes anything (still runs in dry-run mode)
    remember_last: str = ""  # "disk" / "memory": offline, answer with the last good result (tools/last_known.py)


@dataclass
class ToolResult:
    text: str
    error: bool = False


ConfirmFn = Callable[[Tool, dict], bool]


def wait_for(check: Callable[[], object], timeout: float = 4.0, every: float = 0.4):
    """Poll until check() returns something truthy (returned) or time runs out (None). Action tools use it to
    confirm the result really happened before JARVIS says so ("Checked: now playing ...")."""
    import time

    end = time.monotonic() + timeout
    while True:
        try:
            got = check()
        except Exception:
            got = None
        if got or time.monotonic() >= end:
            return got or None
        time.sleep(every)
AuditFn = Callable[[str, dict, str, str], None]

MAX_OUTPUT_CHARS = 3500  # more makes the model slower to answer


class ToolRegistry:
    def __init__(self, audit: AuditFn | None = None, dry_run: bool = False):
        self._tools: dict[str, Tool] = {}
        self._audit = audit or (lambda *a: None)
        self.dry_run = dry_run
        # Called after every tool request with (name, args, status); the UI uses it to show
        # what really happened, independent of what the model claims.
        self.on_call: Callable[[str, dict, str], None] | None = None
        # security.no_approval: everyday CONFIRM tools that just run. DANGER tools always ask.
        self.no_approval: set[str] = set()
        self.trace: list | None = None
        self._local = threading.local()
        self.confirm_fn: ConfirmFn | None = None  # the app's approval prompt, for tools that ask mid-way

    def needs_approval(self, tool: Tool) -> bool:
        if tool.risk is Risk.SAFE:
            return False
        return tool.risk is Risk.DANGER or tool.name not in self.no_approval

    @contextlib.contextmanager
    def asking(self, fn: Callable[[str], bool]):
        """In this thread, questions from inside tools go to fn instead of the app's approval prompt."""
        before, self._local.ask = getattr(self._local, "ask", None), fn
        try:
            yield
        finally:
            self._local.ask = before

    def ask(self, what: str) -> bool:
        """Ask the user about one specific risky step inside an otherwise approval-free tool
        (clicking 'Buy now', pressing Alt+F4). No approval prompt available = no."""
        if own := getattr(self._local, "ask", None):  # a background job: it can't use the app's prompt
            return bool(own(what))
        if not self.confirm_fn:
            return False
        step = Tool("risky_step", "A risky step", {"type": "object", "properties": {}, "required": []},
                    Risk.DANGER, "screen", lambda: None)
        return bool(self.confirm_fn(step, {"action": what}))

    def tool(
        self,
        name: str,
        description: str,
        properties: dict,
        required: list[str] | None = None,
        risk: Risk = Risk.SAFE,
        group: str = "core",
        read_only: bool = False,
        remember_last: str = "",
    ):
        """Decorator: register a function as a model-callable tool."""
        if read_only and risk is not Risk.SAFE:
            raise ValueError("read_only tools must be SAFE")
        params = {
            "type": "object",
            "properties": properties,
            "required": required if required is not None else list(properties),
        }

        def deco(func):
            if name in self._tools:
                raise ValueError(f"Tool {name!r} registered twice")
            self._tools[name] = Tool(name, description, params, risk, group, func, read_only, remember_last)
            return func

        return deco

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, groups: set[str] | None = None) -> list[dict]:
        """OpenAI-style function schemas (accepted by Ollama and OpenAI-compatible APIs)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
            if groups is None or t.group in groups
        ]

    def execute(self, name: str, args: Any, confirm: ConfirmFn) -> ToolResult:
        result, parsed_args, status = self._execute(name, args, confirm)
        if self.trace is not None:  # this turn's tool calls, for learning playbooks from clean successes
            self.trace.append((name, parsed_args, status, (result.text or "")[:300]))
        if self.on_call:
            self.on_call(name, parsed_args, status)
        return result

    def _execute(self, name: str, args: Any, confirm: ConfirmFn) -> tuple[ToolResult, dict, str]:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", error=True), {}, "invalid"

        if isinstance(args, str):  # some APIs send arguments as a JSON string
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                return ToolResult("Arguments were not valid JSON.", error=True), {}, "invalid"
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return ToolResult("Arguments must be an object.", error=True), {}, "invalid"

        # Small models sometimes invent extra arguments; drop anything undeclared.
        args = {k: v for k, v in args.items() if k in tool.parameters["properties"]}
        missing = [k for k in tool.parameters["required"] if k not in args]
        if missing:
            return ToolResult(f"Missing required arguments: {missing}", error=True), args, "invalid"

        if self.needs_approval(tool) and not confirm(tool, args):
            self._audit(name, args, tool.risk.value, "declined")
            return ToolResult("The user declined this action. Do not retry it.", error=True), args, "declined"

        if self.dry_run and not tool.read_only:
            self._audit(name, args, tool.risk.value, "dry-run")
            logger.info("tool={} status=dry-run args={}", name, args)
            msg = f"[dry run] Nothing was changed. {name} was only simulated. Tell the user it was a simulation."
            return ToolResult(msg), args, "dry-run"

        start = time.perf_counter()
        try:
            out, status = tool.func(**args), "ok"
        except PermissionError as e:
            out, status = f"Permission denied: {e}", "denied"
        except Exception as e:  # report failures to the model instead of crashing
            logger.exception("Tool {} failed", name)
            out, status = f"{type(e).__name__}: {e}", "error"
        elapsed = (time.perf_counter() - start) * 1000
        logger.info("tool={} status={} {:.0f}ms args={}", name, status, elapsed, args)
        self._audit(name, args, tool.risk.value, status)

        if isinstance(out, str):
            text = out
        else:
            text = json.dumps(out, default=str, ensure_ascii=False)
        if tool.remember_last and self.last_known is not None:
            text, status = self._offline_fallback(tool, args, text, status)
        return ToolResult(text[:MAX_OUTPUT_CHARS], error=status not in ("ok", "offline")), args, status

    last_known = None  # LastKnown (set by app.build)

    def _offline_fallback(self, tool: Tool, args: dict, text: str, status: str) -> tuple[str, str]:
        """Remember good results; when the internet is down, answer with the last one (saying how old it is)."""
        from jarvis.tools.last_known import internet_up

        failed = status != "ok" or text.startswith("ERROR")
        if not failed:
            self.last_known.put(tool.name, args, text, tool.remember_last)
            return text, status
        if internet_up():
            return text, status  # a real error, not the connection: never cover it with old data
        if old := self.last_known.get(tool.name, args):
            logger.info("tool={} offline: answered with the last known result", tool.name)
            return old, "offline"
        return "ERROR: there's no internet connection right now, and I have no earlier result for this.", status
