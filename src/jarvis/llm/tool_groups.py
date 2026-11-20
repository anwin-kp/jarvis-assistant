"""Pick which tool groups to send per request. Small local models choose tools more
reliably when they see ~10 relevant tools instead of every tool."""

KEYWORDS: dict[str, list[str]] = {
    "system": [
        "volume", "mute", "sound", "brightness", "lock", "process", "cpu", "ram", "memory usage",
        "battery", "shutdown", "restart", "reboot", "powershell", "command", "ip address", "network",
        "open", "close", "launch", "start", "quit", "kill", "app", "program",
    ],
    "files": [
        "file", "folder", "document", "pdf", "docx", "photo", "picture", "download", "desktop",
        "delete", "move", "rename", "find my", "where is",
    ],
    "web": [
        "search", "google", "look up", "weather", "temperature", "rain", "news", "headline",
        "who is", "who was", "what is", "what's", "when is", "latest", "today", "price", "score",
        "wiki", "http", "www", "website",
    ],
    "memory": ["remember", "forget", "recall", "my name", "my favorite", "do you know", "facts", "about me"],
}

ALWAYS = {"core", "memory"}


def pick_groups(text: str) -> set[str]:
    t = text.lower()
    groups = set(ALWAYS) | {g for g, words in KEYWORDS.items() if any(w in t for w in words)}
    if groups == ALWAYS:
        groups.add("web")  # general questions often need a search
    return groups
