"""Let the model store and look up facts about the user."""

from jarvis.memory.store import Memory
from jarvis.tools.registry import Risk, ToolRegistry


def register(reg: ToolRegistry, memory: Memory) -> None:
    @reg.tool(
        "remember",
        "Permanently save a fact or preference about the user, e.g. 'Prefers Celsius'. "
        "You MUST call this whenever the user asks you to remember something; nothing is saved otherwise.",
        {"fact": {"type": "string", "description": "The fact, written in third person"}},
        group="memory",
    )
    def remember(fact: str):
        return memory.remember(fact)

    @reg.tool(
        "recall",
        "Search saved facts and past conversations (including earlier days).",
        {"query": {"type": "string"}},
        group="memory",
        read_only=True,
    )
    def recall(query: str):
        try:
            by_meaning = memory.semantic_recall(query)
        except Exception:
            by_meaning = []
        hits = list(dict.fromkeys(by_meaning + memory.recall(query)))  # meaning first, then exact words
        return hits[:12] or "Nothing found."

    @reg.tool(
        "list_facts",
        "List all saved facts about the user, with ids. Call this when asked what you know about the user.",
        {},
        group="memory",
        read_only=True,
    )
    def list_facts():
        return memory.list_facts() or "No saved facts."

    @reg.tool(
        "forget",
        "Delete a saved fact by its id (get ids from list_facts).",
        {"fact_id": {"type": "integer"}},
        risk=Risk.CONFIRM,
        group="memory",
    )
    def forget(fact_id: int):
        return memory.forget(int(fact_id))
