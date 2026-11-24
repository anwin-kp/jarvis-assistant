"""Pick which tool groups to send per request. Small local models choose tools more
reliably when they see ~10 relevant tools instead of every tool."""

import re

KEYWORDS: dict[str, list[str]] = {
    "system": [
        "volume", "mute", "sound", "loud", "louder", "quiet", "quieter", "softer", "can't hear", "turn it up",
        "turn it down", "brightness", "brighter", "dimmer", "too bright", "lock", "process", "cpu", "ram",
        "memory usage",
        "battery", "shutdown", "restart", "reboot", "powershell", "command", "ip address", "network",
        "open", "close", "launch", "start", "quit", "kill", "app", "program", "internet", "online", "offline",
        "connection", "connected", "ping", "my ip", "headset", "headphones", "speakers", "earbuds", "earphones",
        "audio output", "audio device", "sound output", "microphone", "mic", "startup", "starts with windows",
        "windows update", "cpu temp", "gpu temp", "overheat", "running hot", "battery health", "battery wear",
    ],
    "files": [
        "file", "folder", "document", "pdf", "docx", "photo", "picture", "download", "desktop",
        "delete", "move", "rename", "find my", "where is", "recent", "latest file", "last file", "just downloaded",
        "duplicate", "large files", "big files", "biggest files", "free up space", "disk space", "organize", "sort my",
        "zip", "unzip", "extract",
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
    "email": ["mark as read", "as read", "as unread", "archive", "trash", "inbox",
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
                "close the", "bring up", "go to", "side by side", "split screen", "arrange", "layout", "on the left",
                "on the right", "next to each other", "workspace", "work mode", "focus mode"],
    "clipboard": ["clipboard", "copied", "copy", "paste", "type ", "write this", "translate", "summarize this",
                  "selected", "selection", "highlighted", "rewrite", "rephrase", "proofread", "this text"],
    "pc": ["dark mode", "light mode", "dark", "my eyes", "theme", "wifi", "wi-fi", "bluetooth", "screenshot",
           "screen shot",
           "recycle bin", "settings", "airplane"],
    "notes": ["note", "notes", "journal", "jot", "write down", "diary"],
    "screen": ["screen", "error", "this window", "what am i looking at", "read this", "on my monitor", "look at",
               "see", "selected", "click", "button", "press the", "in the app", "type in", "fill in", "in spotify",
               "in chrome", "in the browser", "go to", "navigate", "open the", "settings", "shortcut",
               "press ", " app and ", "in it ", "in calculator", "in notepad", "in paint", "in explorer",
               "in file explorer", "in settings", "in the settings", "in vs code", "in word", "in excel"],
    "docs": ["document", "documents", "pdf", "agreement", "contract", "invoice", "report", "resume", "cv",
             "according to", "my files", "policy", "manual", "in my notes", "lease", "say about", "says about",
             "statement", "warranty"],
    "browser": ["website", "browser", "web page", "webpage", "site", "click", "fill", "form", "sign up", "log in",
                "order", "book a", "on amazon", "on flipkart", "checkout"],
    "tabs": ["tab", "tabs"],
    "apps": ["install", "uninstall", "update my apps", "update all", "updates", "upgrade", "winget",
             "new version", "out of date", "outdated"],
    "code": ["code", "coding", "project", "repo", "repository", "git", "commit", "branch", "diff", "unit test",
             "run the tests", "run tests", "failing test", "test fail", "build the", "build apk", "compile", "analyze",
             "analyzer", "lint", "flutter", "dart", "python", "function", "class ", "refactor", "bug", "fix the",
             "vs code", "source file", "codebase", "pull request", "stack trace"],
    "mission": ["mission", "in the background", "step by step", "research", "compare"],
    "utility": ["calculate", "plus", "minus", "times", "multiplied", "divided", "percent", "square root",
                "squared", "how much is", "convert", "in km", "in miles", "kg", "pounds", "celsius", "fahrenheit",
                "degrees", "inches", "feet", "cm", "liters", "litres", "ml", "cups", "mph", "km/h", "gb", "mb",
                "currency", "exchange rate", "dollar", "rupee", "usd", "inr", "eur", "euro", "gbp",
                "days until", "days since", "days left", "how many days", "days from", "weeks until",
                "define", "definition", "meaning of", "what does", "synonym"],
    "memory": ["remember", "forget", "recall", "my name", "my favorite", "do you know", "facts", "about me"],
    "interview": ["know me", "get to know", "interview me", "about myself", "learn about me", "ask me about"],
}

def add_keywords(group: str, words: list[str]) -> None:
    """Plugins and MCP servers register the words that should bring their tools into play."""
    KEYWORDS.setdefault(group, [])
    KEYWORDS[group].extend(w.lower() for w in words if w)


ALWAYS = {"core", "memory"}


def _mentions(text: str, word: str) -> bool:
    """Keyword at the start of a word ('remind' matches 'reminder', 'event' does NOT match 'prevent');
    short keywords (<= 3 letters, e.g. 'ac', 'tv') must be whole words."""
    w = word.strip()
    pattern = r"(?<![a-z0-9])" + re.escape(w) + (r"(?![a-z0-9])" if len(w) <= 3 else "")
    return re.search(pattern, text) is not None


MATH = re.compile(r"\d\s*(?:[-+*/x×÷^]\s*[\d(]|%)")
DOMAIN = re.compile(r"\b[a-z0-9-]+\.(?:com|in|org|net|io|co|ai|dev|app|me|uk|gov|edu|info|tv)\b")


def pick_groups(text: str) -> set[str]:
    t = text.lower()
    groups = set(ALWAYS) | {g for g, words in KEYWORDS.items() if any(_mentions(t, w) for w in words)}
    if DOMAIN.search(t):  # "open amazon.in and search..." is a website task
        groups.add("browser")
    if MATH.search(t):  # "15% of 2400", "12*7": exact maths
        groups.add("utility")
    if groups == ALWAYS:
        groups.add("web")  # general questions often need a search
    return groups
