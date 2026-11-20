from PIL import Image

from jarvis.ui import attachments


def use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(attachments, "KNOWLEDGE", tmp_path / "knowledge")


def test_save_sanitises_and_resolve_stays_inside(monkeypatch, tmp_path):
    use_tmp(monkeypatch, tmp_path)
    p = attachments.save("..\\..\\evil<>.txt", b"hi")
    assert p.parent == tmp_path / "knowledge" and p.name.endswith("evil_.txt")
    assert attachments.resolve(p.name) == p.resolve()
    assert attachments.resolve("../../secrets.txt") is None
    assert attachments.display_name(p) == "evil_.txt"


def test_compose_includes_text_and_writes_searchable_copy(monkeypatch, tmp_path):
    use_tmp(monkeypatch, tmp_path)
    p = attachments.save("notes.csv", b"name,score\nanwin,10\n")
    out = attachments.compose("summarise this", [p], {})
    assert "anwin,10" in out and out.rstrip().endswith("searched later.)") and "summarise this" in out
    assert (p.parent / (p.name + ".md")).read_text(encoding="utf-8").startswith("# notes.csv")


def test_compose_describes_images(monkeypatch, tmp_path):
    use_tmp(monkeypatch, tmp_path)
    (tmp_path / "x.png").write_bytes(b"")
    Image.new("RGB", (20, 20), "red").save(tmp_path / "x.png")
    p = attachments.save("shot.png", (tmp_path / "x.png").read_bytes())
    monkeypatch.setattr(attachments, "describe_image", lambda path, q, cfg: f"a red square ({q})")
    out = attachments.compose("what colour?", [p], {})
    assert "a red square (what colour?)" in out and "image 'shot.png'" in out


def test_too_big(monkeypatch, tmp_path):
    use_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(attachments, "MAX_MB", 0)
    try:
        attachments.save("a.txt", b"x")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_routing_uses_the_users_words_not_the_attachment():
    from jarvis.core.orchestrator import Orchestrator

    seen = {}

    class Brain:
        cloud = object()
        last_tool_calls: list = []

        def respond(self, messages, tools, confirm, **kw):
            seen.update(kw, content=messages[-1]["content"])
            return "ok"

    orch = Orchestrator.__new__(Orchestrator)
    orch.history, orch.last_activity, orch.idle_seconds, orch.history_turns = [], 0, 1e9, 6
    orch.brain, orch.semantic = Brain(), None
    orch.system_prompt = lambda: "sys"
    orch.reg = type("R", (), {"schemas": lambda self, g: [], "execute": lambda *a: None})()
    orch.memory = type("M", (), {"log_turn": lambda *a: None})()
    long_doc = "screenshot of my files and documents " * 20
    orch.handle(long_doc + "\n\nwhat is this?", lambda *a: True, route_text="what is this?")
    assert "think" not in seen and seen["private"] is False and "what is this?" in seen["content"]
    orch.handle(long_doc, lambda *a: True, route_text="summarise", private=True)
    assert seen["private"] is True
