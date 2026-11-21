from jarvis.integrations import google
from jarvis.tools.registry import ToolRegistry


class FakeGmail:
    """Just enough of the Gmail API: an inbox of unread ids and batchModify."""

    def __init__(self, unread, fail_permission=False):
        self.unread, self.fail = set(unread), fail_permission

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        ids = sorted(self.unread)
        return type("Q", (), {"execute": lambda s: {"messages": [{"id": i} for i in ids]}})()

    def batchModify(self, userId, body):
        if self.fail:
            raise RuntimeError("Request had insufficient authentication scopes.")
        if "UNREAD" in body.get("removeLabelIds", []):
            self.unread -= set(body["ids"])
        return type("Q", (), {"execute": lambda s: {}})()


def tools(monkeypatch, gmail):
    monkeypatch.setattr(google, "_service", lambda *a: gmail)
    reg = ToolRegistry()
    google.register(reg)
    return reg


def test_mark_all_unread_as_read_and_check(monkeypatch):
    gm = FakeGmail({"a", "b", "c"})
    r = tools(monkeypatch, gm).execute("mark_emails_read", {"ids": "all"}, lambda *a: True)
    assert "Marked 3 email(s) as read (checked: 0 unread left" in r.text and not gm.unread


def test_old_read_only_sign_in_gets_clear_instructions(monkeypatch):
    r = tools(monkeypatch, FakeGmail({"a"}, fail_permission=True)).execute("mark_emails_read", {"ids": "a"},
                                                                          lambda *a: True)
    assert "google-login" in r.text and r.text.startswith("ERROR")


def test_trash_needs_approval_and_mark_read_does_not(monkeypatch):
    reg = tools(monkeypatch, FakeGmail(set()))
    reg.no_approval = {"mark_emails_read", "archive_email"}
    assert reg.needs_approval(reg.get("trash_email"))
    assert not reg.needs_approval(reg.get("mark_emails_read"))
