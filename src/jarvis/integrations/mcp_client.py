"""MCP client: connect JARVIS to any Model Context Protocol server (stdio), e.g. GitHub, filesystem,
browser, Notion... Each server's tools become JARVIS tools. Configure under mcp_servers in config.yaml."""

import asyncio
import os
import re
import threading

from loguru import logger

from jarvis.llm import tool_groups
from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.web_tools import untrusted


class MCPManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.sessions: dict = {}
        self._stop: asyncio.Event | None = None
        threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp").start()

    def _run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _serve(self, name: str, spec: dict, ready: asyncio.Future):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=spec["command"], args=spec.get("args", []),
                                       env={**os.environ, **spec.get("env", {})})
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                self.sessions[name] = session
                ready.set_result(tools)
                self._stop = self._stop or asyncio.Event()
                await self._stop.wait()
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.exception("MCP server {} stopped", name)

    def connect(self, name: str, spec: dict):
        async def start():
            ready = self.loop.create_future()
            self.loop.create_task(self._serve(name, spec, ready))
            return await asyncio.wait_for(ready, 60)

        return self._run(start(), timeout=70)

    def call(self, server: str, tool: str, args: dict) -> str:
        result = self._run(self.sessions[server].call_tool(tool, args))
        parts = [getattr(c, "text", None) or f"[{getattr(c, 'type', 'content')}]" for c in result.content]
        text = "\n".join(parts) or "(no output)"
        is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)  # MCP v2 / v1
        return ("ERROR: " if is_error else "") + text

    def close(self) -> None:
        if self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)


def register(reg: ToolRegistry, servers: dict) -> MCPManager | None:
    if not servers:
        return None
    mgr = MCPManager()
    for name, spec in servers.items():
        try:
            tools = mgr.connect(name, spec)
        except Exception as e:
            logger.error("MCP server {} failed to start: {}", name, e)
            continue
        risk = Risk.SAFE if spec.get("trust") == "read_only" else Risk.CONFIRM
        group = f"mcp_{name}"
        tool_groups.add_keywords(group, list(spec.get("keywords", [])) + [name])
        for t in tools:
            tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{name}__{t.name}")[:64]
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}

            def make(server=name, tname=t.name):
                def call(**kwargs):
                    return untrusted(f"mcp:{server}", mgr.call(server, tname, kwargs)[:6000])
                return call

            reg.tool(tool_name, (t.description or t.name)[:400], schema.get("properties", {}),
                     required=schema.get("required", []), risk=risk, group=group,
                     read_only=risk is Risk.SAFE)(make())
        logger.info("MCP server {} connected: {} tools", name, len(tools))
    return mgr
