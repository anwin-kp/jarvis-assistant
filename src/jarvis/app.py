"""Wires all components together."""

from pathlib import Path

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
    from jarvis.tools import clipboard_tools, media_tools, notes_tools, pc_tools, window_tools

    media_tools.register(reg)
    window_tools.register(reg)
    clipboard_tools.register(reg)
    pc_tools.register(reg)
    from jarvis.core.config import ROOT
    from jarvis.core.plugins import load_plugins
    from jarvis.integrations import mcp_client
    from jarvis.tools import screen_tools

    reg.plugins = load_plugins(reg, ROOT / "plugins", cfg, memory, DATA)
    reg.mcp = mcp_client.register(reg, cfg.get("mcp_servers") or {})

    screen_tools.register(reg)
    dcfg = cfg.get("documents", {})
    if dcfg.get("enabled", True):
        import ollama

        from jarvis.docs.index import DocIndex, ollama_embedder
        from jarvis.docs.index import register as register_docs

        folders = [file_tools.resolve_root(f) for f in dcfg.get("folders", ["documents"])]
        embed = ollama_embedder(ollama.Client(host=cfg["llm"]["ollama_host"]), dcfg.get("model", "nomic-embed-text"))
        reg.doc_index = DocIndex(memory, [f for f in folders if f.exists()], embed)
        register_docs(reg, reg.doc_index)
    notes_dir = cfg.get("notes", {}).get("folder") or str(file_tools.resolve_root("documents") / "JARVIS Notes")
    notes_tools.register(reg, Path(notes_dir).expanduser())
    memory_tools.register(reg, memory)
    from jarvis.integrations import google

    if google.is_configured():
        google.register(reg)

    local = OllamaBrain(reg, cfg["llm"])
    ccfg = cfg["llm"].get("cloud") or {}
    cloud = None
    if ccfg.get("provider"):
        from jarvis.llm.cloud_brain import CloudBrain

        cloud = CloudBrain(reg, ccfg["provider"], ccfg.get("model", ""), cfg["llm"].get("max_steps", 6))
    from jarvis.llm.cloud_brain import Router

    brain = Router(local, cloud, reg, cfg["llm"].get("mode", "local"), ccfg.get("use_in_gaming_mode", True))
    from jarvis.integrations import home_assistant

    home_assistant.register(reg, cfg.get("home_assistant") or {})
    orch = Orchestrator(brain, reg, memory, load_persona(cfg.get("persona")), cfg.get("history_turns", 8),
                        cfg.get("idle_summary_minutes", 10))
    return cfg, orch, brain, reg, memory, roots
