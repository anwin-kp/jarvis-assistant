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


def register_interview(reg: ToolRegistry, orch) -> None:
    """'Get to know me': JARVIS asks about the user one question at a time and saves the answers."""

    @reg.tool("start_interview", "Start getting to know the user: when they say 'get to know me', 'ask me about "
              "myself', 'interview me', 'learn about me'. Then ask your first question.", {}, group="interview")
    def start_interview():
        orch.interview = True
        known = len(memory_facts(orch))
        return (f"Get-to-know-you mode is on ({known} facts known so far). Now greet them briefly and ask your "
                "first question about a topic you don't know yet.")

    @reg.tool("end_interview", "End get-to-know-you mode (the user said stop / that's enough, or all topics "
              "are covered).", {}, group="interview")
    def end_interview():
        orch.interview = False
        return f"Get-to-know-you mode is off. You now know {len(memory_facts(orch))} facts about the user."


def memory_facts(orch) -> list:
    return orch.memory.list_facts()
