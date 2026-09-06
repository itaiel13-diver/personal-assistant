import base64
import logging
import os
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# readonly + compose.
#
# IMPORTANT, and verified the hard way against the live API: gmail.compose DOES
# allow sending - drafts().send() succeeds with this scope. An earlier version of
# this comment claimed the opposite and it was wrong. There is no Gmail scope that
# grants draft creation without also granting the ability to send.
#
# So the "never send by itself" guarantee does NOT come from the token. It comes
# from this module exposing no sending function at all: the assistant is given
# create_email_draft and nothing else, and drafts().send() is never called
# anywhere in this codebase. Adding such a call would silently remove the only
# thing standing between the model and a real outgoing email.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")

_service = None


def _gmail_service():
    global _service
    if _service is None:
        if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
            raise RuntimeError("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN are not set")
        credentials = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        _service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return _service


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Gmail bodies are a nested part tree; plain text can sit at any depth, and a
    multipart message carries no body of its own. Walk it and prefer text/plain."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if body_data and mime == "text/plain":
        return _decode(body_data)

    html_fallback = ""
    for part in payload.get("parts", []) or []:
        found = _extract_body(part)
        if found:
            if part.get("mimeType") == "text/html" and not html_fallback:
                html_fallback = found
            else:
                return found
    if html_fallback:
        return html_fallback
    if body_data:
        return _decode(body_data)
    return ""


def search_emails(query: str = "is:unread", max_results: int = 10) -> str:
    """Searches Itai's work inbox and returns matching emails as a list of
    sender / date / subject lines, each ending with [id:...].
    query uses Gmail's own search syntax, for example 'is:unread',
    'from:samsung', 'newer_than:3d', 'subject:יעדים'.
    Use read_email with an id from this list to see the full text of one."""
    # Logged because the only record of a tool actually firing in production is the
    # server log, and a successful Gmail call is otherwise indistinguishable from
    # the model answering about mail without ever looking.
    logger.info(f"Gmail tool: search_emails(query={query!r}, max_results={max_results})")
    try:
        service = _gmail_service()
        listing = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = listing.get("messages", [])
        if not messages:
            return f"לא נמצאו מיילים עבור החיפוש: {query}"

        lines = []
        for ref in messages:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            lines.append(
                f"{_header(msg, 'Date')} | {_header(msg, 'From')} | "
                f"{_header(msg, 'Subject')} [id:{ref['id']}]"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Gmail search failed: {e}")
        return f"❌ שגיאה בחיפוש מיילים: {e}"


def read_email(message_id: str) -> str:
    """Returns the full text of one email. Get message_id from search_emails,
    which prints it as [id:...] after each result."""
    logger.info(f"Gmail tool: read_email(message_id={message_id!r})")
    try:
        service = _gmail_service()
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        body = _extract_body(msg.get("payload", {}))
        # Long threads blow up the prompt and the cost; the model can ask for more.
        if len(body) > 4000:
            body = body[:4000] + "\n[...הודעה ארוכה, נחתכה]"
        return (
            f"מאת: {_header(msg, 'From')}\n"
            f"תאריך: {_header(msg, 'Date')}\n"
            f"נושא: {_header(msg, 'Subject')}\n\n{body}"
        )
    except Exception as e:
        logger.error(f"Gmail read failed: {e}")
        return f"❌ שגיאה בקריאת המייל: {e}"


def create_email_draft(to: str, subject: str, body: str) -> str:
    """Creates a DRAFT email in Itai's Gmail. It is saved to his drafts folder and is
    NOT sent - this function only ever creates drafts. Tell Itai the draft is ready
    and that he needs to open Gmail to review and send it himself.
    Use this whenever Itai asks you to write or reply to an email."""
    logger.info(f"Gmail tool: create_email_draft(to={to!r}, subject={subject!r})")
    try:
        service = _gmail_service()
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId="me", body={"message": {"raw": encoded}}
        ).execute()
        return (
            f"✅ טיוטה נשמרה בג'ימייל (אל: {to} | נושא: {subject}) "
            f"[draft:{draft.get('id', '')}]. היא לא נשלחה - צריך לפתוח את Gmail, לבדוק ולשלוח."
        )
    except Exception as e:
        logger.error(f"Gmail draft failed: {e}")
        return f"❌ שגיאה ביצירת הטיוטה: {e}"
