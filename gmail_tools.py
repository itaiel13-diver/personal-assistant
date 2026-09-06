import base64
import logging
import os
from email.message import EmailMessage

import attachment_readers

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


def _list_attachments(payload: dict) -> list:
    """Collects every attached file in the message.

    Attachment parts are the ones carrying a filename. Unlike a text part they
    hold no `data`: the body is a reference (`attachmentId`) that has to be
    fetched in a second, separate API call. That is why simply walking the tree
    for text - which is all _extract_body does - skips them without a trace."""
    found = []
    if not payload:
        return found
    filename = payload.get("filename") or ""
    body = payload.get("body", {}) or {}
    if filename and body.get("attachmentId"):
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "size": body.get("size", 0),
            "attachment_id": body["attachmentId"],
        })
    for part in payload.get("parts", []) or []:
        found.extend(_list_attachments(part))
    return found


def _describe_attachments(attachments: list) -> str:
    lines = []
    for a in attachments:
        supported = (
            " — ניתן לקריאה" if attachment_readers.is_supported(a["filename"], a["mime_type"])
            else " — לא ניתן לקריאה"
        )
        lines.append(f"  • {a['filename']} ({a['size'] // 1024 or 1}KB){supported}")
    return "\n".join(lines)


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
        payload = msg.get("payload", {})
        body = _extract_body(payload)
        # Long threads blow up the prompt and the cost; the model can ask for more.
        if len(body) > 4000:
            body = body[:4000] + "\n[...הודעה ארוכה, נחתכה]"

        result = (
            f"מאת: {_header(msg, 'From')}\n"
            f"תאריך: {_header(msg, 'Date')}\n"
            f"נושא: {_header(msg, 'Subject')}\n\n{body}"
        )
        attachments = _list_attachments(payload)
        if attachments:
            result += (
                f"\n\n📎 קבצים מצורפים ({len(attachments)}):\n"
                + _describe_attachments(attachments)
                + "\n\nכדי לקרוא אחד מהם, קרא ל-read_email_attachment עם שם הקובץ."
            )
        return result
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


def _locate_attachment(message_id: str, filename: str):
    """Returns (target, raw_bytes, error). Exactly one of target/error is None."""
    service = _gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    attachments = _list_attachments(msg.get("payload", {}))
    if not attachments:
        return None, None, "❌ אין קבצים מצורפים במייל הזה."

    # Matched by name rather than by attachmentId on purpose: the id is a
    # long opaque string that a model transcribes wrongly, while the filename
    # is short, meaningful, and already in front of it from read_email.
    wanted = filename.strip().lower()
    matches = [a for a in attachments if a["filename"].lower() == wanted]
    if not matches:
        matches = [a for a in attachments if wanted in a["filename"].lower()]
    if not matches:
        names = ", ".join(a["filename"] for a in attachments)
        return None, None, f"❌ לא נמצא קובץ בשם '{filename}'. הקבצים במייל הזה: {names}"
    if len(matches) > 1:
        names = ", ".join(a["filename"] for a in matches)
        return None, None, f"❌ יותר מקובץ אחד מתאים ל-'{filename}': {names}. צריך שם מדויק יותר."

    target = matches[0]
    if not attachment_readers.is_supported(target["filename"], target["mime_type"]):
        # Checked before downloading - no point spending the round trip on
        # bytes that cannot be turned into text anyway.
        return None, None, attachment_readers.extract_text(
            target["filename"], b"\x00", target["mime_type"]
        )
    if target["size"] > attachment_readers.MAX_ATTACHMENT_BYTES:
        size_mb = target["size"] / (1024 * 1024)
        return None, None, f"❌ הקובץ {target['filename']} גדול מדי לקריאה ({size_mb:.1f}MB)."

    blob = service.users().messages().attachments().get(
        userId="me", messageId=message_id, id=target["attachment_id"]
    ).execute()
    raw = base64.urlsafe_b64decode(blob["data"].encode("utf-8"))
    return target, raw, None


def read_email_attachment(message_id: str, filename: str, part: int = 1) -> str:
    """Reads the contents of a file attached to an email and returns it as text.
    message_id comes from search_emails, filename from the attachment list that
    read_email prints. Use this whenever Itai asks what is inside an attached
    file, or asks a question that the attached spreadsheet or document answers.
    A long file is returned in numbered parts: the reply says how many there are,
    and part=2, part=3 and so on fetch the rest, so nothing is unreachable. When
    Itai is looking for specific rows rather than the whole file, prefer
    search_email_attachment - it scans every row without paging.
    Supports xlsx, csv, pdf, docx and plain text. Images and scanned PDFs cannot
    be read - say so plainly rather than guessing at their contents."""
    logger.info(
        f"Gmail tool: read_email_attachment(message_id={message_id!r}, "
        f"filename={filename!r}, part={part!r})"
    )
    try:
        target, raw, error = _locate_attachment(message_id, filename)
        if error:
            return error
        try:
            page = int(part)
        except (TypeError, ValueError):
            page = 1
        text = attachment_readers.extract_text(
            target["filename"], raw, target["mime_type"], part=max(1, page)
        )
        return f"📎 תוכן הקובץ {target['filename']}:\n\n{text}"
    except Exception as e:
        logger.error(f"Gmail attachment read failed: {e}")
        return f"❌ שגיאה בקריאת הקובץ המצורף: {e}"


def search_email_attachment(message_id: str, filename: str, keywords: str,
                            must_also_match: str = "") -> str:
    """Searches inside an attached file and returns every row that matches, having
    scanned the whole file with no row limit. Use this instead of
    read_email_attachment whenever Itai wants particular rows rather than the
    whole document - for example rows about a branch or city, or rows assigned to
    a specific person.

    keywords is a comma-separated list matched with OR: a row is a hit if it
    contains ANY of them. Hebrew and English spellings of the cities in Itai's
    territory are expanded automatically, so passing 'ראשון לציון' also finds
    'ראשל"צ' and 'Rishon LeZion', and spelling differences like קריית/קרית do not
    matter.

    must_also_match is a second comma-separated list, ANDed with the first: a row
    must match one keyword AND one of these. Use it to cross-reference - for
    example keywords='רמלה, לוד, יבנה' with must_also_match='איתי, ניקיטה'
    returns only the rows for those cities that also name one of those two
    people. Leave it empty when no cross-reference is wanted."""
    logger.info(
        f"Gmail tool: search_email_attachment(message_id={message_id!r}, "
        f"filename={filename!r}, keywords={keywords!r}, must_also_match={must_also_match!r})"
    )
    try:
        target, raw, error = _locate_attachment(message_id, filename)
        if error:
            return error
        return attachment_readers.search(
            target["filename"], raw, keywords,
            must_also_match=must_also_match, mime_type=target["mime_type"],
        )
    except Exception as e:
        logger.error(f"Gmail attachment search failed: {e}")
        return f"❌ שגיאה בחיפוש בקובץ המצורף: {e}"
