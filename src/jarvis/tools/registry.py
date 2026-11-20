"""Tool registry and permission gate.

The model can only *request* tools. This module decides whether a request runs,
needs the user's approval, or is refused. Permissions are enforced here in code,
never by the prompt.
"""

from __future__ import annotations

import json
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


@dataclass
class ToolResult:
    text: str
    error: bool = False


ConfirmFn = Callable[[Tool, dict], bool]
AuditFn = Callable[[str, dict, str, str], None]

MAX_OUTPUT_CHARS = 6000


class ToolRegistry:
    def __init__(self, audit: AuditFn | None = None, dry_run: bool = False):
        self._tools: dict[str, Tool] = {}
        self._audit = audit or (lambda *a: None)
        self.dry_run = dry_run
        # Called after every tool request with (name, args, status); the UI uses it to show
        # what really happened, independent of what the model claims.
        self.on_call: Callable[[str, dict, str], None] | None = None

    def tool(
        self,
        name: str,
        description: str,
        properties: dict,
        required: list[str] | None = None,
        risk: Risk = Risk.SAFE,
        group: str = "core",
        read_only: bool = False,
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
            self._tools[name] = Tool(name, description, params, risk, group, func, read_only)
            return func

        return deco

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

        if tool.risk is not Risk.SAFE and not confirm(tool, args):
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
        return ToolResult(text[:MAX_OUTPUT_CHARS], error=status != "ok"), args, status
