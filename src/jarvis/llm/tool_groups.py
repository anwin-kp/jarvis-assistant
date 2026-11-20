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
    "reminder": [
        "remind", "reminder", "alarm", "timer", "minutes", "tomorrow", "schedule", "task", "to-do", "todo",
        "to do", "briefing", "agenda",
    ],
    "email": [
        "email", "e-mail", "mail", "inbox", "gmail", "unread", "draft", "reply", "send", "calendar",
        "meeting", "event", "appointment", "schedule", "agenda", "busy", "free on",
    ],
    "home": [
        "light", "lamp", "fan", "switch", "plug", "tv", "ac", "air conditioner", "thermostat", "scene",
        "smart home", "device", "bedroom", "living room", "kitchen",
    ],
    "media": ["play", "pause", "resume", "song", "music", "track", "next", "previous", "skip", "youtube",
              "spotify", "video", "playlist", "album", "listen to"],
    "windows": ["window", "switch to", "focus", "minimize", "maximise", "maximize", "snap", "desktop",
                "close the", "bring up", "go to"],
    "clipboard": ["clipboard", "copied", "copy", "paste", "type ", "write this", "translate", "summarize this"],
    "pc": ["dark mode", "light mode", "theme", "wifi", "wi-fi", "bluetooth", "screenshot", "screen shot",
           "recycle bin", "settings", "airplane"],
    "notes": ["note", "notes", "journal", "jot", "write down", "diary"],
    "screen": ["screen", "error", "this window", "what am i looking at", "read this", "on my monitor"],
    "docs": ["document", "documents", "pdf", "agreement", "contract", "invoice", "report", "resume", "cv",
             "according to", "my files", "policy", "manual", "in my notes"],
    "browser": ["website", "browser", "web page", "webpage", "site", "click", "fill", "form", "sign up", "log in",
                "order", "book a", "on amazon", "on flipkart", "checkout"],
    "memory": ["remember", "forget", "recall", "my name", "my favorite", "do you know", "facts", "about me"],
}

def add_keywords(group: str, words: list[str]) -> None:
    """Plugins and MCP servers register the words that should bring their tools into play."""
    KEYWORDS.setdefault(group, [])
    KEYWORDS[group].extend(w.lower() for w in words if w)


ALWAYS = {"core", "memory"}


def pick_groups(text: str) -> set[str]:
    t = text.lower()
    groups = set(ALWAYS) | {g for g, words in KEYWORDS.items() if any(w in t for w in words)}
    if groups == ALWAYS:
        groups.add("web")  # general questions often need a search
    return groups
