import json

from PIL import Image

from jarvis.agent.operator import Operator, Target, annotate, dedupe, merge, shorten, to_send_keys


def test_keys():
    assert to_send_keys("ctrl+s") == "^s"
    assert to_send_keys("ctrl+shift+t") == "^+t"
    assert to_send_keys("enter") == "{ENTER}"
    assert to_send_keys("alt+f4") == "%{F4}"
    assert to_send_keys("alt+left") == "%{LEFT}"


def test_merge_prefers_accessibility_items_and_keeps_other_text():
    ui = [Target("Button: Play", "ui", (100, 100, 160, 130))]
    text = [Target("Play", "text", (110, 105, 150, 125)), Target("Mix-Up Mash", "text", (10, 300, 120, 320))]
    m = merge(ui, text)
    assert [t.label for t in m] == ["Button: Play", "Mix-Up Mash"]


def test_annotate_draws_on_a_copy():
    img = Image.new("RGB", (200, 200), "black")
    out = annotate(img, [Target("x", "text", (10, 30, 60, 50))], (0, 0))
    assert out is not img and out.getpixel((10, 40)) != (0, 0, 0)


class FakeHTTP:
    def __init__(self, calls):
        self.calls = list(calls)
        self.prompts = []

    def post(self, url, **kw):
        c = kw["json"]["messages"][0]["content"]
        self.prompts.append(c if isinstance(c, str) else c[0]["text"])
        name, args = self.calls.pop(0)
        body = {"choices": [{"message": {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(args)}}]}}]}
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()


def test_decide_parses_the_tool_call_and_lists_targets():
    http = FakeHTTP([("click", {"target": 1})])
    op = Operator("key", http=http)
    img = Image.new("RGB", (100, 100))
    call = op._decide("open playlist", [], img, [Target("Home", "text", (0, 0, 1, 1)),
                                               Target("Mix-Up Mash", "text", (0, 0, 1, 1))])
    assert call == ("click", {"target": 1})
    assert "[1] Mix-Up Mash" in http.prompts[0] and "open playlist" in http.prompts[0]


def test_risky_click_needs_approval():
    asked = []
    op = Operator("key", confirm=lambda what: asked.append(what) or False)
    res = op._act("click", {"target": 0}, [Target("Button: Place order", "ui", (0, 0, 10, 10))], win=None)
    assert res.startswith("STOP") and asked == ["click 'Button: Place order'"]


def test_compact_labels_and_dedupe():
    label = shorten("Button", "Mix-Up   Mash Pinned Playlist and more words here")
    assert label.startswith("btn: Mix-Up Mash Pinned") and len(label) == len("btn: ") + 40
    row = Target("item: Mix-Up Mash", "ui", (30, 424, 586, 552))
    twin = Target("btn: Mix-Up Mash Pinned", "ui", (31, 424, 586, 550))
    off = Target("btn: hidden", "ui", (3000, 10, 3050, 40))
    assert dedupe([row, twin, off], (0, 0, 2000, 1500)) == [row]


def test_text_first_and_look_escalates_to_the_screenshot():
    http = FakeHTTP([("look", {}), ("click", {"target": 0})])
    op = Operator("key", http=http)
    targets = [Target(f"btn: {i}", "ui", (0, 0, 1, 1)) for i in range(20)]
    assert op._decide("t", [], None, targets) == ("look", {})
    assert op._decide("t", [], Image.new("RGB", (50, 50)), targets) == ("click", {"target": 0})


def test_text_model_rejection_falls_back_to_the_screenshot_model():
    import requests

    class Rejecting(FakeHTTP):
        def post(self, url, **kw):
            if kw["json"]["model"] == "openai/gpt-oss-120b":
                r = type("R", (), {"status_code": 400, "text": "tool_use_failed", "headers": {}})()
                r.raise_for_status = lambda: (_ for _ in ()).throw(requests.HTTPError("400"))
                return r
            return super().post(url, **kw)

    op = Operator("key", http=Rejecting([("click", {"target": 0})]))
    targets = [Target(f"btn: {i}", "ui", (0, 0, 1, 1)) for i in range(20)]
    assert op._decide("t", [], None, targets) is None  # -> run() asks the vision model with the screenshot
    assert op._decide("t", [], Image.new("RGB", (50, 50)), targets) == ("click", {"target": 0})
