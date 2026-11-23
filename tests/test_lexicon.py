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


def test_prompt_has_only_said_names_as_one_clean_list():
    """Measured on the user's recorded voice: names never said in the prompt raised the word error rate (19.5% vs
    15.6%) and made 'Hey Jarvis' 'Kali Cervis'; one name after 'No.' made it 'Kali Jarvis' every time."""
    L = lex()
    L.count_said(["play anirudh ravichander please", "play anirudh ravichander"])
    L.fixes["Tamil"] = 3  # corrected often, but never said: stays out
    p = L.prompt("Jarvis, Kochi. Stop. Yes. No.", limit=80)
    assert p == "Jarvis, Kochi, Stop, Yes, No, Anirudh Ravichander."
    assert "Tamil" not in p and len(p) <= 80


def test_names_reach_the_prompt_only_once_said():
    L = lex()
    L.count_said(["hello"])
    assert "Pradeep" not in L.prompt(cloud=True) and "Pradeep" not in L.prompt(cloud=False)
    L.count_said(["send pradeep gupta the report"])
    assert "Pradeep Gupta" in L.prompt(cloud=True)


def test_corrections_count_only_for_accepted_turns():
    """A song on the TV was corrected again and again ('K. Sreekumar' -> 'M. G. Sreekumar') and 'trained' it."""
    L = lex()
    L.add(["Mix-Up Mash"], "playlist")
    assert L.correct("play my mixup mesh", count=False) == "play my Mix-Up Mash"
    assert L.fixes.get("Mix-Up Mash", 0) == 0 and L.last_fixed == [("mixup mesh", "Mix-Up Mash")]
    L.count_fixes(L.last_fixed)  # the turn was accepted
    assert L.fixes["Mix-Up Mash"] == 1
    L.correct("nothing to fix here", count=False)
    assert L.last_fixed == []


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


def test_project_names_are_spoken_and_misheard_ones_corrected():
    """Measured with real Groq Whisper on synthesized commands: 2/14 project names right without them, 14/14 with
    the names in the prompt + correction ('QuickHop Mobile', 'ARC delivery', 'Modbus Coder' were heard)."""
    from jarvis.stt.lexicon import Lexicon
    from jarvis.tools.code_tools import spoken_names

    forms = spoken_names(["ARK_Field_Staff-SHARE", "arkcustomer-SHARE", "ark_sales", "ark_delivery", "QuickOP",
                          "QuickOP-backend", "QuickOP-mobile", "modbus_codec", "anwin_kp_portfolio"])
    assert forms["Ark Field Staff Share"] == "ARK_Field_Staff-SHARE" and forms["Ark Field Staff"]
    assert forms["Ark Customer Share"] == "arkcustomer-SHARE"  # 'ark' is a word of other projects: split
    assert forms["Quick OP Backend"] == "QuickOP-backend" and "Quick OP" in forms
    lex = Lexicon()
    lex.add(list(forms), "project")
    for heard, right in [("Fix the login bug in QuickHop Mobile.", "Fix the login bug in Quick OP Mobile."),
                         ("Any analyzer errors in ARC delivery?", "Any analyzer errors in Ark Delivery?"),
                         ("What's new in Modbus Coder?", "What's new in Modbus Codec?"),
                         ("Open Amun KP Portfolio in VS Code.", "Open Anwin KP Portfolio in VS Code."),  # whole name
                         ("Where is the cart screening Arcfield staff?",
                          "Where is the cart screening Ark Field Staff?")]:
        assert lex.correct(heard) == right, heard
    assert lex.correct("run the tests and show me") == "run the tests and show me"  # ordinary words untouched


def test_projects_are_found_however_they_are_said(tmp_path):
    import subprocess

    from jarvis.tools import code_tools
    from jarvis.tools.registry import ToolRegistry

    for name in ("QuickOP-backend", "modbus_codec"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "-q", str(tmp_path / name)], check=True)
    reg = ToolRegistry()
    code_tools.register(reg, {"coding": {"search_roots": [str(tmp_path)]}})
    assert reg.code_project("Quick OP Backend").path.name == "QuickOP-backend"
    assert reg.code_project("Modbus Codec").path.name == "modbus_codec"
    assert reg.code_project("modbus-codec").path.name == "modbus_codec"
