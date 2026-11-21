import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

from jarvis.core.plugins import load_plugins
from jarvis.core.proactive import Proactive
from jarvis.integrations import mcp_client
from jarvis.llm.tool_groups import pick_groups
from jarvis.memory.store import Memory
from jarvis.tools.registry import Risk, ToolRegistry

OK = lambda t, a: True
ROOT = Path(__file__).resolve().parents[1]


def test_example_plugin_loads(tmp_path):
    reg = ToolRegistry()
    assert "dice_and_time" in load_plugins(reg, ROOT / "plugins", {}, Memory(tmp_path / "m.db"), tmp_path)
    r = reg.execute("roll_dice", {"sides": 6, "count": 3}, OK)
    assert '"total"' in r.text
    assert "UTC" in reg.execute("world_time", {"place": "UTC"}, OK).text
    assert "plugin_dice_and_time" in pick_groups("roll two dice for me")


def test_broken_plugin_is_skipped(tmp_path):
    (tmp_path / "bad.py").write_text("raise RuntimeError('boom')")
    (tmp_path / "good.py").write_text("def register(reg, ctx):\n    @reg.tool('hello', 'hi', {})\n"
                                      "    def hello():\n        return 'hi'\n")
    reg = ToolRegistry()
    assert load_plugins(reg, tmp_path, {}, Memory(tmp_path / "m.db"), tmp_path) == ["good"]


def test_break_and_meeting_reminders():
    alerts, idle = [], [0]
    start = datetime.now().astimezone() + timedelta(minutes=8)
    p = Proactive(alerts.append, battery_percent=0, disk_free_gb=0, break_minutes=60, idle_fn=lambda: idle[0],
                  events_fn=lambda m: [("e1", "Standup", start)], meeting_minutes=10)
    p.check(now=0)
    assert alerts == ["Standup starts in 8 minutes."]
    p.check(now=30 * 60)
    p.check(now=61 * 60)
    assert alerts[-1].startswith("You've been working for 1 hour")
    idle[0] = 600  # away for 10 minutes resets the work timer
    p.check(now=62 * 60)
    idle[0] = 0
    p.check(now=63 * 60)
    p.check(now=100 * 60)
    assert len(alerts) == 2  # neither the meeting nor the break repeats


def test_mcp_server_tools(tmp_path):
    server = tmp_path / "srv.py"
    server.write_text(textwrap.dedent("""
        from mcp.server.mcpserver import MCPServer
        mcp = MCPServer("demo")

        @mcp.tool()
        def add(a: int, b: int) -> int:
            \"\"\"Add two numbers.\"\"\"
            return a + b

        mcp.run()
    """))
    reg = ToolRegistry()
    mgr = mcp_client.register(reg, {"demo": {"command": sys.executable, "args": [str(server)], "trust": "read_only"}})
    try:
        assert reg.get("demo__add").risk is Risk.SAFE
        assert "5" in reg.execute("demo__add", {"a": 2, "b": 3}, OK).text
        assert "mcp_demo" in pick_groups("ask demo to add numbers")
    finally:
        mgr.close()
