"""Plugins: drop a .py file into the plugins/ folder to give JARVIS new tools.

A plugin defines   def register(reg, ctx): ...   and uses @reg.tool(...) exactly like the built-in tools.
Optional: KEYWORDS = ["word", ...] so JARVIS offers the plugin's tools when you say those words.
ctx has: cfg (config dict), memory, data_dir (a private folder for the plugin).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from jarvis.llm import tool_groups


def load_plugins(reg, folder: Path, cfg: dict, memory, data_root: Path) -> list[str]:
    loaded = []
    if not folder.exists():
        return loaded
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            data_dir = data_root / "plugins" / name
            data_dir.mkdir(parents=True, exist_ok=True)
            before = set(reg.names())
            mod.register(reg, SimpleNamespace(cfg=cfg, memory=memory, data_dir=data_dir))
            group = getattr(mod, "GROUP", f"plugin_{name}")
            for tool_name in set(reg.names()) - before:
                tool = reg.get(tool_name)
                if tool.group == "core":  # plugins default to their own group
                    tool.group = group
            tool_groups.add_keywords(group, list(getattr(mod, "KEYWORDS", [])) + [name.replace("_", " ")])
            loaded.append(name)
            logger.info("Plugin loaded: {} ({} tools)", name, len(set(reg.names()) - before))
        except Exception:
            logger.exception("Plugin {} failed to load", name)
    return loaded
