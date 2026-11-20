from jarvis.llm.tool_groups import pick_groups
from jarvis.memory.store import Memory
from jarvis.tools.web_tools import is_public_url


def test_memory_facts_and_recall(tmp_path):
    m = Memory(tmp_path / "t.db")
    assert m.remember("Prefers Celsius") == "Saved."
    assert m.remember("Prefers Celsius") == "I already knew that."
    m.log_turn("my project is called Falcon", "Noted, Falcon.")
    hits = m.recall("Falcon project")
    assert any("Falcon" in h for h in hits)
    assert "Celsius" in m.profile()
    fid = m.list_facts()[0]["id"]
    assert m.forget(fid) == "Forgotten."
    assert m.list_facts() == []


def test_tool_groups():
    assert "system" in pick_groups("set the volume to 30")
    assert "files" in pick_groups("find my resume pdf")
    assert "web" in pick_groups("what's the weather in Kochi")
    assert "web" in pick_groups("tell me a joke")  # default fallback


def test_ssrf_guard():
    assert not is_public_url("http://127.0.0.1:11434/api/tags")
    assert not is_public_url("http://192.168.1.1/")
    assert not is_public_url("file:///C:/Windows/win.ini")
