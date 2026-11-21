"""Learning from a document the user says is about them (resume, LinkedIn PDF)."""
from jarvis.__main__ import ABOUT_ME
from jarvis.memory.learner import good_doc_fact, supported

KNOWN = ["[work] Built and published an EV charging app alone in 6 months (Flutter, BLoC, Razorpay wallets)."]


def test_profile_facts_need_no_first_person_but_must_quote_the_document():
    doc = "Senior Flutter Developer at Gadgeon Smart Systems since August 2022."
    item = {"fact": "[work] Senior Flutter Developer at Gadgeon Smart Systems.",
            "evidence": "Senior Flutter Developer at Gadgeon"}
    assert supported(item, doc, document=True) and not supported(item, doc)
    assert not supported({**item, "evidence": "Lead engineer at Google"}, doc, document=True)  # invented


def test_fragments_other_and_known_facts_are_dropped():
    assert not good_doc_fact("[other] QuickOP – Healthcare app", KNOWN)
    assert not good_doc_fact("[work] Set up Clean Archi", KNOWN)
    assert not good_doc_fact("[work] Built and published the EV charging app alone (Flutter, BLoC, Razorpay).", KNOWN)
    assert good_doc_fact("[work] Holds an AWS Certified Developer certification since March 2025.", KNOWN)


def test_about_me_requests():
    for t in ["learn about me from this", "this is my LinkedIn profile", "remember my resume", "my details"]:
        assert ABOUT_ME.search(t), t
    assert not ABOUT_ME.search("summarise this file")
