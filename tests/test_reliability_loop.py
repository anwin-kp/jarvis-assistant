"""The reliability loop: weekly failures -> reviewed eval cases (personal, never committed) -> every eval run."""

import yaml

from jarvis import review

TURNS = [
    {"user": "Mark all of them as red", "reply": "I cannot mark emails as red.", "tools": [],
     "signals": ["empty", "repeated"], "next": "Mark all of them as red."},
    {"user": "Mark all of them as red", "reply": "same", "tools": [], "signals": ["repeated"], "next": ""},  # duplicate
    {"user": "Hello.", "reply": "Hello!", "tools": [], "signals": ["repeated"], "next": ""},  # too short to matter
    {"user": "play my liked songs", "reply": "Stopped.", "tools": ["spotify_play:ok"], "signals": ["stopped"],
     "next": ""},  # only interrupted: not a failure
    {"user": "close the youtube tab", "reply": "Couldn't.", "tools": ["close_browser_tabs:error"],
     "signals": ["tool_error"], "next": ""},
]


def test_flagged_turns_become_candidates_to_review():
    cands = review.candidates(TURNS)
    assert [c["say"] for c in cands] == ["Mark all of them as red", "close the youtube tab"]
    assert cands[0]["expect"] == ["?"] and cands[0]["got"] == ["none"] and cands[1]["got"] == ["close_browser_tabs"]


def test_only_reviewed_cases_are_added_once(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "MY_CASES", tmp_path / "evals" / "my_cases.yaml")
    path = tmp_path / "eval-candidates-20261121.yaml"
    assert review.write_candidates(TURNS, path) == 2 and path.read_text(encoding="utf-8").startswith("# Turns")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data[0]["expect"] = ["mark_emails_read"]  # reviewed; the second keeps '?' (skipped)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert review.add_cases(path) == (1, path)
    assert review.add_cases(path) == (0, path)  # no duplicates
    mine = yaml.safe_load(review.MY_CASES.read_text(encoding="utf-8"))
    assert mine == [{"say": "Mark all of them as red", "expect": ["mark_emails_read"]}]


def test_personal_cases_join_every_eval(tmp_path, monkeypatch):
    from jarvis import evals

    monkeypatch.setattr(review, "MY_CASES", tmp_path / "my.yaml")
    public = len(evals.load_cases())
    (tmp_path / "my.yaml").write_text(yaml.safe_dump([{"say": "mark them as read", "expect": ["mark_emails_read"]}]),
                                      encoding="utf-8")
    cases = evals.load_cases()
    assert len(cases) == public + 1 and cases[-1]["mine"] and len(evals.load_cases(mine=False)) == public


def test_weekly_rate_against_the_target(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "HISTORY", tmp_path / "history.json")
    assert review.record_week(200, 44) == " (22%, target < 15%)"
    assert review.record_week(180, 18) == " (10%, target < 15%; last week 22%)"
