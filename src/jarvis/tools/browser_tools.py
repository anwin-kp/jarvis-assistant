"""Browser agent: JARVIS drives its own Edge window (separate profile) to open pages, read, click and type.
Every click/type needs your approval, and it refuses to type into password fields."""

from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.web_tools import is_public_url, untrusted


class Browser:
    def __init__(self, profile_dir):
        self.profile_dir = profile_dir
        self._pw = self._ctx = self.page = None

    def _ensure(self):
        if self.page and not self.page.is_closed():
            return self.page
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), channel="msedge", headless=False, no_viewport=True, args=["--start-maximized"])
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self.page

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._ctx = self.page = None

    def summary(self) -> str:
        p = self.page
        text = p.inner_text("body")[:4500]
        return untrusted(p.url, f"Title: {p.title()}\nURL: {p.url}\n\n{text}")


def register(reg: ToolRegistry, profile_dir) -> Browser:
    b = Browser(profile_dir)

    @reg.tool("browser_open", "Open a web page in JARVIS's browser window (for tasks that need clicking or typing "
              "on a site). Returns the page text. To just open a site for the user ('open X in Chrome'), use "
              "open_in_browser.", {"url": {"type": "string"}}, group="browser")
    def browser_open(url: str):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not is_public_url(url):
            raise PermissionError("Only public http(s) sites are allowed")
        page = b._ensure()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return b.summary()

    @reg.tool("browser_read", "Read the current page in JARVIS's browser.", {}, group="browser", read_only=True)
    def browser_read():
        if not b.page:
            return "ERROR: the browser isn't open. Use browser_open first."
        return b.summary()

    @reg.tool("browser_click", "Click a link or button on the current page by its visible text.",
              {"text": {"type": "string"}}, risk=Risk.CONFIRM, group="browser")
    def browser_click(text: str):
        from jarvis.agent.operator import RISKY

        if RISKY.search(text) and not reg.ask(f"click '{text}' on the web page"):
            return f"Not clicked: '{text}' needs your approval and you didn't give it."
        page = b._ensure()
        for loc in (page.get_by_role("button", name=text), page.get_by_role("link", name=text),
                    page.get_by_text(text)):
            if loc.count():
                loc.first.click(timeout=8000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return b.summary()
        return f"ERROR: nothing clickable with text '{text}'."

    @reg.tool("browser_type", "Type into a field on the current page, found by its label or placeholder. "
              "Set submit=true to press Enter afterwards.",
              {"field": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
              required=["field", "text"], risk=Risk.CONFIRM, group="browser")
    def browser_type(field: str, text: str, submit: bool = False):
        page = b._ensure()
        loc = page.get_by_label(field).or_(page.get_by_placeholder(field)).or_(page.get_by_role("textbox", name=field))
        if not loc.count():
            return f"ERROR: no field called '{field}'."
        el = loc.first
        if (el.get_attribute("type") or "").lower() == "password":
            raise PermissionError("I never type passwords. Please enter it yourself.")
        el.fill(text, timeout=8000)
        if submit:
            el.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        return b.summary() if submit else f"Typed into '{field}'."

    @reg.tool("browser_close", "Close JARVIS's browser window.", {}, group="browser")
    def browser_close():
        b.close()
        return "Browser closed."

    return b
