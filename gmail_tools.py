import base64
import logging
import os
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# readonly + compose only. compose can create and update drafts but CANNOT send,
# so the assistant is physically incapable of emailing anyone by mistake - the
# safety rule in the system prompt is enforced by the token, not by good behaviour.
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
    """Creates a DRAFT email in Itai's Gmail. It is only saved to his drafts folder -
    this cannot and will not send anything. Tell Itai the draft is ready and that he
    needs to open Gmail to review and send it himself.
    Use this whenever Itai asks you to write or reply to an email."""
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
