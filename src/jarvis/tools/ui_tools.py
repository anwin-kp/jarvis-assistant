"""Acting in any app: read a window's buttons, lists, menus and fields through Windows UI Automation and
click or type into them. Clicking and typing always ask first."""

from jarvis.tools.registry import Risk, ToolRegistry

INTERESTING = {"Button", "MenuItem", "ListItem", "TabItem", "Hyperlink", "CheckBox", "RadioButton", "TreeItem",
               "DataItem", "Edit", "ComboBox", "SplitButton", "Text"}
MAX_ELEMENTS = 120


def _window(name: str | None):
    from pywinauto import Desktop

    if name:
        from jarvis.tools.window_tools import find_window

        w = find_window(name)
        if not w:
            raise LookupError(f"no open window matching '{name}'")
        hwnd = w["hwnd"]
    else:
        from jarvis.tools.vision_tools import target_window

        hwnd = target_window()
        if not hwnd:
            raise LookupError("no active window")
    return Desktop(backend="uia").window(handle=hwnd)


def elements(win) -> list:
    out = []
    for el in win.descendants():
        try:
            info = el.element_info
            ctype, name = info.control_type, (info.name or "").strip()
            if ctype in INTERESTING and name and el.is_visible():
                out.append(el)
        except Exception:
            continue
        if len(out) >= MAX_ELEMENTS:
            break
    return out


def describe(els: list) -> list[str]:
    return [f"[{i}] {el.element_info.control_type}: {el.element_info.name[:80]}" for i, el in enumerate(els)]


def find(els: list, name: str):
    """Exact name first, then contains; index like '12' also works."""
    if name.strip().isdigit() and int(name) < len(els):
        return els[int(name)]
    n = name.lower().strip()
    exact = [e for e in els if e.element_info.name.lower().strip() == n]
    if exact:
        return exact[0]
    part = [e for e in els if n in e.element_info.name.lower()]
    return part[0] if part else None


def register(reg: ToolRegistry) -> None:
    @reg.tool("list_ui_elements", "List the clickable/readable items (buttons, list items, menus, fields) in a window. "
              "window: app or title, empty = the window the user is working in.",
              {"window": {"type": "string"}}, required=[], group="screen", read_only=True)
    def list_ui_elements(window: str = ""):
        els = elements(_window(window or None))
        return describe(els) or "No accessible items found in that window."

    @reg.tool("click_ui_element", "Click an item in a window by its visible name (or [index] from list_ui_elements). "
              "double=true to open/play list items.",
              {"name": {"type": "string"}, "window": {"type": "string"}, "double": {"type": "boolean"}},
              required=["name"], risk=Risk.CONFIRM, group="screen")
    def click_ui_element(name: str, window: str = "", double: bool = False):
        win = _window(window or None)
        el = find(elements(win), name)
        if el is None:
            return f"ERROR: no item named '{name}'. Use list_ui_elements to see what's there."
        win.set_focus()
        if double:
            el.click_input(double=True)
        else:
            try:
                el.invoke()
            except Exception:
                el.click_input()
        return f"Clicked {el.element_info.control_type} '{el.element_info.name}'."

    @reg.tool("type_into_ui_field", "Type text into a field in a window (found by its name), optionally press Enter.",
              {"field": {"type": "string"}, "text": {"type": "string"}, "window": {"type": "string"},
               "enter": {"type": "boolean"}},
              required=["field", "text"], risk=Risk.CONFIRM, group="screen")
    def type_into_ui_field(field: str, text: str, window: str = "", enter: bool = False):
        win = _window(window or None)
        el = find([e for e in elements(win) if e.element_info.control_type in ("Edit", "ComboBox")], field)
        if el is None:
            return f"ERROR: no field named '{field}'."
        win.set_focus()
        el.click_input()
        try:
            el.set_edit_text(text)  # sets the value directly (any characters)
        except Exception:
            from jarvis.tools.clipboard_tools import type_unicode

            type_unicode(text)
        if enter:
            el.type_keys("{ENTER}")
        return f"Typed into '{el.element_info.name}'."
