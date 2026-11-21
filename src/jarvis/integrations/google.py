"""Gmail + Google Calendar (free APIs, your own Google account).

Setup (one time): put your OAuth 'Desktop app' client file at data/google/credentials.json, then run
    .\\jarvis.ps1 google-login
Least-privilege scopes: read mail, create drafts (sending needs your approval), manage calendar events.
"""

import base64
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from loguru import logger

from jarvis.core.config import DATA
from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.web_tools import untrusted

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read + mark read/unread, archive, trash (never delete)
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]
FOLDER = DATA / "google"
CREDENTIALS = FOLDER / "credentials.json"
TOKEN = FOLDER / "token.json"


def is_configured() -> bool:
    return CREDENTIALS.exists() and TOKEN.exists()


def missing_scopes() -> set[str]:
    """Permissions JARVIS needs that the saved sign-in doesn't have."""
    import json

    try:
        granted = set(json.loads(TOKEN.read_text(encoding="utf-8")).get("scopes") or [])
    except (OSError, ValueError):
        return set(SCOPES)
    return set(SCOPES) - granted


def get_credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if interactive and creds and missing_scopes():
        creds = None  # signed in before a permission was added (e.g. gmail.modify): ask Google for it now
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:  # e.g. refresh token expired (7 days in OAuth 'Testing' mode)
            logger.warning("Google token refresh failed: {}", e)
    if not interactive:
        raise PermissionError("Google sign-in expired. Run: .\\jarvis.ps1 google-login")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")  # always show the permission list
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(name: str, version: str):
    from googleapiclient.discovery import build

    return build(name, version, credentials=get_credentials(), cache_discovery=False)


def _header(msg: dict, name: str) -> str:
    return next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == name), "")


def _body_text(payload: dict) -> str:
    """Find the first text/plain part of a Gmail message."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        if text := _body_text(part):
            return text
    return ""


def _day_range(day: str) -> tuple[datetime, datetime]:
    d = day.strip().lower()
    today = date.today()
    start = {"today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1)}.get(d)
    if start is None:
        start = date.fromisoformat(day[:10])
    begin = datetime.combine(start, datetime.min.time()).astimezone()
    return begin, begin + timedelta(days=1)


def register(reg: ToolRegistry) -> None:
    @reg.tool("list_unread_emails", "List recent unread emails (sender, subject, snippet, id).",
              {"max_results": {"type": "integer", "description": "1-15, default 5"}}, required=[],
              group="email", read_only=True, remember_last="memory")  # email: never written to disk
    def list_unread_emails(max_results: int = 5):
        gm = _service("gmail", "v1")
        ids = gm.users().messages().list(userId="me", q="is:unread in:inbox",
                                         maxResults=max(1, min(int(max_results), 15))).execute().get("messages", [])
        out = []
        for m in ids:
            msg = gm.users().messages().get(userId="me", id=m["id"], format="metadata",
                                            metadataHeaders=["From", "Subject", "Date"]).execute()
            out.append(f"- id={m['id']} | from {_header(msg, 'from')} | {_header(msg, 'subject')} | "
                       f"{msg.get('snippet', '')[:150]}")
        return untrusted("gmail", "\n".join(out) or "No unread emails.")

    @reg.tool("read_email", "Read the full text of one email by id.", {"email_id": {"type": "string"}},
              group="email", read_only=True)
    def read_email(email_id: str):
        msg = _service("gmail", "v1").users().messages().get(userId="me", id=email_id, format="full").execute()
        text = f"From: {_header(msg, 'from')}\nSubject: {_header(msg, 'subject')}\n\n{_body_text(msg['payload'])}"
        return untrusted("gmail", text[:5000])

    def _modify(action):
        """Run a mailbox change; an old sign-in (read-only) gets a clear instruction instead of an error."""
        try:
            return action()
        except Exception as e:
            if "insufficient" in str(e).lower() or getattr(getattr(e, "resp", None), "status", 0) == 403:
                return ("ERROR: JARVIS doesn't have permission to change emails yet. The user needs to sign in "
                        "again once: .\\jarvis.ps1 google-login")
            raise

    def _unread_ids(gm, limit: int = 500) -> list[str]:
        ids, token = [], None
        while len(ids) < limit:
            page = gm.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=100,
                                              pageToken=token).execute()
            ids += [m["id"] for m in page.get("messages", [])]
            token = page.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def _ids(gm, ids: str) -> list[str]:
        if ids.strip().lower() in ("all", "all unread", "all_unread", "everything", "every"):
            return _unread_ids(gm)
        return [i.strip() for i in ids.replace(";", ",").split(",") if i.strip()]

    @reg.tool("mark_emails_read", "Mark emails as read (or unread with read=false). ids: comma-separated ids from "
              "list_unread_emails, or 'all' for every unread email in the inbox.",
              {"ids": {"type": "string"}, "read": {"type": "boolean", "description": "default true"}},
              required=["ids"], group="email")
    def mark_emails_read(ids: str, read: bool = True):
        def go():
            gm = _service("gmail", "v1")
            id_list = _ids(gm, ids)
            if not id_list:
                return "No unread emails to mark." if read else "No email ids given."
            key = "removeLabelIds" if read else "addLabelIds"
            gm.users().messages().batchModify(userId="me", body={"ids": id_list[:1000], key: ["UNREAD"]}).execute()
            left = len(_unread_ids(gm))
            return (f"Marked {len(id_list)} email(s) as {'read' if read else 'unread'} "
                    f"(checked: {left} unread left in the inbox).")
        return _modify(go)

    @reg.tool("archive_email", "Archive emails (remove from the inbox; still in All Mail). ids: comma-separated ids.",
              {"ids": {"type": "string"}}, group="email")
    def archive_email(ids: str):
        def go():
            gm = _service("gmail", "v1")
            id_list = _ids(gm, ids)
            gm.users().messages().batchModify(userId="me", body={"ids": id_list, "removeLabelIds": ["INBOX"]}).execute()
            return f"Archived {len(id_list)} email(s) (still in All Mail)."
        return _modify(go)

    @reg.tool("trash_email", "Move emails to the Trash (recoverable for 30 days). ids: comma-separated ids.",
              {"ids": {"type": "string"}}, risk=Risk.CONFIRM, group="email")
    def trash_email(ids: str):
        def go():
            gm = _service("gmail", "v1")
            id_list = [i for i in _ids(gm, ids) if i]
            for i in id_list:
                gm.users().messages().trash(userId="me", id=i).execute()
            return f"Moved {len(id_list)} email(s) to the Trash (recoverable for 30 days)."
        return _modify(go)

    @reg.tool("draft_email", "Create an email draft (not sent). Returns the draft id.",
              {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
              risk=Risk.CONFIRM, group="email")
    def draft_email(to: str, subject: str, body: str):
        m = EmailMessage()
        m["To"], m["Subject"] = to, subject
        m.set_content(body)
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        d = _service("gmail", "v1").users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return f"Draft created (id {d['id']}) to {to}: {subject}"

    @reg.tool("send_draft", "Send an existing draft by its draft id.", {"draft_id": {"type": "string"}},
              risk=Risk.DANGER, group="email")
    def send_draft(draft_id: str):
        _service("gmail", "v1").users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return "Email sent."

    @reg.tool("list_events", "List calendar events for a day: 'today', 'tomorrow' or a date (YYYY-MM-DD).",
              {"day": {"type": "string"}}, group="email", read_only=True, remember_last="memory")
    def list_events(day: str):
        begin, end = _day_range(day)
        items = _service("calendar", "v3").events().list(
            calendarId="primary", timeMin=begin.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=25).execute().get("items", [])
        rows = []
        for e in items:
            s = e["start"].get("dateTime", e["start"].get("date"))
            rows.append(f"- {s[11:16] if 'T' in s else 'all day'} {e.get('summary', '(no title)')}"
                        + (f" @ {e['location']}" if e.get("location") else ""))
        return untrusted("calendar", "\n".join(rows) or f"No events on {begin:%a %d %b}.")

    @reg.tool("create_event", "Add a calendar event. start_iso is local time, e.g. 2026-11-21T13:00:00.",
              {"title": {"type": "string"}, "start_iso": {"type": "string"},
               "duration_minutes": {"type": "integer", "description": "default 60"},
               "location": {"type": "string"}},
              required=["title", "start_iso"], risk=Risk.CONFIRM, group="email")
    def create_event(title: str, start_iso: str, duration_minutes: int = 60, location: str = ""):
        start = datetime.fromisoformat(start_iso).astimezone()
        end = start + timedelta(minutes=int(duration_minutes))
        body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        if location:
            body["location"] = location
        e = _service("calendar", "v3").events().insert(calendarId="primary", body=body).execute()
        return f"Event created: {title} on {start:%a %d %b %H:%M} ({e.get('htmlLink', '')})"


def upcoming_events(minutes: float = 15) -> list[tuple[str, str, datetime]]:
    """Timed events starting within `minutes` (used for proactive meeting reminders)."""
    now = datetime.now().astimezone()
    items = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=(now + timedelta(minutes=minutes)).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=10).execute().get("items", [])
    out = []
    for e in items:
        if "dateTime" in e["start"]:
            out.append((e["id"], e.get("summary", "A meeting"), datetime.fromisoformat(e["start"]["dateTime"])))
    return out


def login() -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    if not CREDENTIALS.exists():
        print(f"Missing {CREDENTIALS}\nCreate an OAuth 'Desktop app' client in Google Cloud Console, download the "
              "JSON and save it there. See README > Email & calendar.")
        sys.exit(1)
    get_credentials(interactive=True)
    print("Signed in to Google. JARVIS can now use Gmail and Calendar.")


if __name__ == "__main__":
    login()
