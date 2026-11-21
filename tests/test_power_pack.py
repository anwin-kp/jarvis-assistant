from jarvis.llm.ollama_brain import SentenceStream
from jarvis.tools import notes_tools
from jarvis.tools.media_tools import first_youtube_video
from jarvis.tools.registry import ToolRegistry

OK = lambda t, a: True


def test_sentence_stream_emits_early_and_holds_bluffs():
    out = []
    s = SentenceStream(out.append, lambda: False)
    s.feed("It's 29 degrees in Ko")
    s.feed("chi. Light rain is")
    assert out == ["It's 29 degrees in Kochi."]  # first sentence spoken while the model is still writing
    s.feed(" likely later. Let me know if")
    s.feed(" you need more.")
    s.finish()
    assert out == ["It's 29 degrees in Kochi.", "Light rain is likely later."]  # filler dropped
    out2 = []
    b = SentenceStream(out2.append, lambda: False)
    b.feed("I've added milk to your list. Anything else? ")
    b.finish()
    assert out2 == []  # claimed action with no tool run: held back


def test_youtube_parse():
    assert first_youtube_video('xx"videoId":"dQw4w9WgXcQ"yy"videoId":"aaaaaaaaaaa"') == "dQw4w9WgXcQ"


def test_notes(tmp_path):
    reg = ToolRegistry()
    notes_tools.register(reg, tmp_path)
    assert reg.execute("take_note", {"text": "buy a new SSD"}, OK).text.startswith("Noted")
    reg.execute("take_note", {"text": "ideas for HUD", "title": "Project: JARVIS"}, OK)
    assert "SSD" in reg.execute("search_notes", {"query": "ssd"}, OK).text
    names = reg.execute("list_notes", {}, OK).text
    assert "Project JARVIS" in names
