"""Long replies are shortened when spoken (the screen shows everything), unless the user asked for detail."""

import queue
import types

from jarvis.__main__ import App, spoken_budget

CFG = {"voice": {"max_spoken_words": 12}}
LONG = ("Kerala is a state on the south-west coast of India. It is known for its backwaters and beaches. "
        "It has the highest literacy rate in India. Its capital is Thiruvananthapuram.")


def test_budget_only_when_no_detail_was_asked():
    assert spoken_budget("tell me about Kerala", CFG) == 12
    for t in ["explain how vaccines work", "tell me five facts about Kerala", "give me 3 tips", "in detail please",
              "read it out", "Give me my daily briefing in a few short spoken sentences"]:
        assert spoken_budget(t, CFG) is None, t
    assert spoken_budget("tell me about Kerala", {"voice": {"max_spoken_words": 0}}) is None


def speak(reply: str, budget, streamed: int = 0) -> list[str]:
    said = []
    me = types.SimpleNamespace(_budget=budget, _spoken_words=0, _cut=False, _barged=False, _streamed=[],
                               _first_word=1.0, _state=lambda s: None, _speech_q=queue.Queue())
    me._within_budget = lambda s: App._within_budget(me, s)
    me._say = lambda t: said.append(t) if t else None
    from jarvis.llm.ollama_brain import SENTENCE_END

    for s in SENTENCE_END.split(reply)[:streamed]:  # sentences the model streamed while writing
        App._speak_async(me, s)
    App._finish_speech(me, reply)
    return [me._speech_q.get_nowait() for _ in range(me._speech_q.qsize())] + said


def test_long_reply_is_cut_at_a_sentence_with_a_pointer_to_the_screen():
    assert speak(LONG, 12) == ["Kerala is a state on the south-west coast of India. It is known for its backwaters "
                               "and beaches. More on screen."]
    assert speak(LONG, 12, streamed=4) == ["Kerala is a state on the south-west coast of India.",
                                           "It is known for its backwaters and beaches.", "More on screen."]


def test_short_or_detailed_replies_are_spoken_whole():
    assert speak("It's 7 PM.", 12) == ["It's 7 PM."]
    assert speak(LONG, None) == [LONG]
