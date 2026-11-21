"""Personal vocabulary: ranked prompt within Whisper's limit, private names kept local, strict corrections."""
from jarvis.memory.personal_data import _clean_name
from jarvis.stt.lexicon import Lexicon


def lex():
    L = Lexicon()
    L.add(["Mix-Up Mash", "Tamil", "Favs"], "playlist")
    L.add(["Arijit Singh", "Anirudh Ravichander"], "artist")
    L.add(["Pradeep Gupta"], "person", private=True)
    L.add(["Notepad", "Spotify"], "app")
    return L


def test_misheard_names_are_corrected_and_neighbours_kept():
    L = lex()
    assert L.correct("Play my mixup mesh playlist.") == "Play my Mix-Up Mash playlist."
    assert L.correct("Play Arjit Sing on Spotify") == "Play Arijit Singh on Spotify"
    assert L.correct("play arijit singh") == "play arijit singh"  # heard right: untouched
    assert L.fixes["Mix-Up Mash"] == 1 and L.fixes["Arijit Singh"] == 1


def test_ordinary_sentences_are_never_changed():
    L = lex()
    for s in ["tell me a joke about cats", "I am going to the market tomorrow", "play some music",
              "how many unread emails do I have", "set the volume to 25", "remind me to call mom"]:
        assert L.correct(s) == s


def test_prompt_ranks_said_and_misheard_names_within_the_limit():
    L = lex()
    L.count_said(["play anirudh ravichander please", "play anirudh ravichander"])
    L.fixes["Tamil"] = 3
    p = L.prompt("Jarvis", limit=60)
    assert len(p) <= 60 and p.startswith("Jarvis")
    assert p.index("Tamil") < p.index("Anirudh Ravichander") if "Anirudh Ravichander" in p else "Tamil" in p


def test_private_names_reach_the_cloud_only_once_said():
    L = lex()
    L.count_said(["hello"])
    assert "Pradeep" not in L.prompt(cloud=True) and "Pradeep Gupta" in L.prompt(cloud=False)
    L.count_said(["send pradeep gupta the report"])
    assert "Pradeep Gupta" in L.prompt(cloud=True)


def test_email_display_names_are_cleaned():
    assert _clean_name('"Pradeep Gupta (Siemens)"') == "Pradeep Gupta"
    assert _clean_name("no-reply") is None and _clean_name("someone@example.com") is None
    assert _clean_name("Support Team") is None


def test_common_words_never_merge_into_a_name():
    L = Lexicon()
    L.add(["WhatsApp", "Anwin"], "config")
    assert L.correct("what's a good name for a cat") == "what's a good name for a cat"
    assert L.correct("why are you saying hedge and win?") == "why are you saying hedge and win?"
    assert L.correct("You have no  You have no") == "You have no  You have no"  # untouched, spacing too


def test_sentence_starts_are_not_names():
    from jarvis.__main__ import proper_names

    assert proper_names("Audio setup: a Bluetooth speaker. Core skills include Flutter.") == ["Bluetooth", "Flutter"]
    assert "Siemens Goa" in proper_names("Works on Siemens Goa projects.")
