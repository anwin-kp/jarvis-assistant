"""Wires all components together."""

from jarvis.core.config import DATA, ensure_data_dirs, load_config, load_persona
from jarvis.core.orchestrator import Orchestrator
from jarvis.llm.ollama_brain import OllamaBrain
from jarvis.memory.store import Memory
from jarvis.tools import app_tools, file_tools, memory_tools, shell_tools, system_tools, web_tools
from jarvis.tools.registry import ToolRegistry


def build(dry_run: bool = False):
    ensure_data_dirs()
    cfg = load_config()
    memory = Memory(DATA / "jarvis.db")
    reg = ToolRegistry(audit=memory.audit, dry_run=dry_run)

    system_tools.register(reg)
    system_tools.register_power(reg)
    if cfg["security"].get("shell_enabled"):
        shell_tools.register(reg)
    app_tools.register(reg, cfg["security"]["app_allowlist"])
    roots = file_tools.register(reg, cfg["security"]["allowed_roots"])
    web_tools.register(reg, cfg.get("web", {}).get("rss_feeds", []))
    memory_tools.register(reg, memory)

    brain = OllamaBrain(reg, cfg["llm"])
    orch = Orchestrator(brain, reg, memory, load_persona(), cfg.get("history_turns", 8))
    return cfg, orch, brain, reg, memory, roots
