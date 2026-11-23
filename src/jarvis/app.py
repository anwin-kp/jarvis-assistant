"""Wires all components together."""

from pathlib import Path

from loguru import logger

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
    from jarvis.tools.last_known import LastKnown

    reg.last_known = LastKnown(DATA / "last-known.json")  # offline: last good weather/news/email answers
    reg.no_approval = set((cfg.get("security") or {}).get("no_approval") or [])

    system_tools.register(reg)
    system_tools.register_power(reg)
    if cfg["security"].get("shell_enabled"):
        shell_tools.register(reg)
    app_tools.register(reg, cfg["security"]["app_allowlist"])
    roots = file_tools.register(reg, cfg["security"]["allowed_roots"])
    web_tools.register(reg, cfg.get("web", {}).get("rss_feeds", []))
    from jarvis.tools import location_tools

    locator = location_tools.register(reg, cfg)
    from jarvis.tools import clipboard_tools, media_tools, notes_tools, pc_tools, window_tools

    media_tools.register(reg)
    window_tools.register(reg)
    from jarvis.tools import desk_tools, tab_tools, utility_tools

    tab_tools.register(reg)  # the user's own Chrome/Edge tabs: list, switch, close (others / duplicates)
    utility_tools.register(reg)  # exact maths, units, currency, dates, definitions
    from jarvis.tools import code_tools

    code_tools.register(reg, cfg)  # your code projects: read, search, git, run tests/analyze/build
    desk_tools.register(reg)  # network status, recent files, open a site in the user's browser
    clipboard_tools.register(reg)
    pc_tools.register(reg)
    from jarvis.core.config import ROOT
    from jarvis.core.plugins import load_plugins
    from jarvis.integrations import mcp_client
    from jarvis.tools import screen_tools

    reg.plugins = load_plugins(reg, ROOT / "plugins", cfg, memory, DATA)
    reg.mcp = mcp_client.register(reg, cfg.get("mcp_servers") or {})

    screen_tools.register(reg)
    from jarvis.integrations import spotify
    from jarvis.tools import ui_tools, vision_tools

    vision_tools.register(reg, cfg)
    ui_tools.register(reg)
    from jarvis.tools import operator_tools

    operator_tools.register(reg, cfg)
    reg.spotify = spotify.setup(reg)
    if reg.spotify:  # the real Spotify tools replace the "open a search" fallback
        reg.unregister("search_spotify")
    if cfg.get("browser", {}).get("enabled", True):
        from jarvis.tools import browser_tools

        reg.browser = browser_tools.register(reg, DATA / "browser-profile")
    dcfg = cfg.get("documents", {})
    import ollama

    from jarvis.docs.index import ollama_embedder

    embed = ollama_embedder(ollama.Client(host=cfg["llm"]["ollama_host"]), dcfg.get("model", "nomic-embed-text"))
    reg.embed = embed
    memory.embed = embed  # recall by meaning
    if dcfg.get("enabled", True):
        from jarvis.docs.index import DocIndex
        from jarvis.docs.index import register as register_docs
        from jarvis.ui.attachments import KNOWLEDGE

        KNOWLEDGE.mkdir(parents=True, exist_ok=True)  # files given in the HUD: always searchable
        folders = [file_tools.resolve_root(f) for f in dcfg.get("folders", ["documents"])] + [KNOWLEDGE]
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
        from jarvis.llm.cloud_brain import CloudBrain, CloudChain

        steps = cfg["llm"].get("max_steps", 6)
        chain = [CloudBrain(reg, ccfg["provider"], ccfg.get("model", ""), steps)]
        for fb in ccfg.get("fallbacks") or []:  # e.g. Gemini when Groq's free limit is reached
            extra = {k: v for k, v in fb.items() if k not in ("provider", "model", "timeout")}
            # a short timeout: when a fallback is having a slow spell, the local model answers instead
            chain.append(CloudBrain(reg, fb["provider"], fb.get("model", ""), steps, timeout=fb.get("timeout", 25),
                                    extra=extra))
        cloud = CloudChain(chain)
    from jarvis.llm.cloud_brain import Router

    brain = Router(local, cloud, reg, cfg["llm"].get("mode", "local"), ccfg.get("use_in_gaming_mode", True))
    from jarvis.core import gaming
    from jarvis.scheduler import reminders

    gcfg = cfg.get("gaming", {})
    reg.gaming = gaming.GamingMode(brain, gcfg.get("processes", []), gcfg.get("auto_detect", True))
    gaming.register(reg, reg.gaming)
    bcfg = cfg.get("briefing", {})
    from jarvis.core.proactive import idle_seconds

    reg.reminders = reminders.Reminders(memory, lambda kind, text: None,
                                        bcfg.get("time") if bcfg.get("enabled", True) else None,
                                        briefing_until=bcfg.get("until", "14:00"),
                                        present_fn=lambda: idle_seconds() < 60)  # briefing when you sit down
    reminders.register(reg, reg.reminders)

    from jarvis.integrations import home_assistant

    home_assistant.register(reg, cfg.get("home_assistant") or {})
    if (cfg.get("coding") or {}).get("private"):  # keep code on this PC: coding questions use the local model only
        from jarvis.llm import cloud_brain

        cloud_brain.PRIVATE_GROUPS.add("code")
    if hasattr(brain, "share_personal"):
        brain.share_personal = bool((cfg.get("memory") or {}).get("share_with_cloud", False))
    orch = Orchestrator(brain, reg, memory, load_persona(cfg.get("persona")), cfg.get("history_turns", 8),
                        cfg.get("idle_summary_minutes", 10))
    orch.history_file = DATA / "conversation.json"  # the conversation continues after a restart
    if n := orch.restore(cfg.get("history_keep_hours", 12)):
        logger.info("Continuing the conversation ({} messages)", n)
    from jarvis.tools.vision_tools import active_window_text

    orch.window_fn = active_window_text  # "what's this error?": which window "this" is (local prompts only)
    if (cfg.get("location") or {}).get("in_prompt", True):
        orch.location_fn = locator.city  # "Panaji, Goa, India": lets "near me" questions work
    if cfg["llm"].get("semantic_tools", True):
        from jarvis.llm.semantic import SemanticGroups

        orch.semantic = SemanticGroups(reg, embed)
    memory_tools.register_interview(reg, orch)
    from jarvis.tasks import playbooks

    reg.playbooks = playbooks.Playbooks(embed, set(reg.names()) | {"find_task_playbook"})
    playbooks.register(reg, reg.playbooks)
    return cfg, orch, brain, reg, memory, roots
