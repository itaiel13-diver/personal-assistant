"""Google Drive for the assistant: reads widely, writes in one folder, can bin a file.

Why this is OAuth as Itai and not the service account. The calendar works
through a service account because a calendar can be *shared* with one. Drive
cannot be made to work that way for what Itai asked for: a service account is a
separate Google identity with its own empty Drive, and a file somebody shared
with itaiel13@gmail.com was not shared with it. "Files shared with me" is only
reachable by acting as him, so this module reuses the Gmail OAuth client and its
refresh token - the same consent, widened.

WHAT THE TOKEN ALLOWS AND WHAT THIS MODULE ALLOWS ARE NOT THE SAME THING, and
the gap is deliberate. Google has no scope that means "read everything, write
in one folder". The narrow write scope (drive.file) is blind to files the app
did not itself create, which would have excluded every file shared with him -
the half he asked for by name. So the token carries the full drive scope and
the restraint lives here, in code, exactly as it does for mail: gmail.compose
can send, and the assistant cannot, because gmail_tools exposes no way to.

The three rules this module keeps:

  1. Removing a file means the bin, not destruction. trash_drive_file sets
     trashed=true, which Drive keeps recoverable for 30 days and which Itai can
     undo himself from the Drive UI without asking anyone. Permanent deletion
     exists behind an explicit permanent=True, and it is the one call in this
     module with no undo, so its tool description tells the model to confirm in
     words before using it that way.

     This rule used to read "it never deletes and never trashes". Itai changed
     it on 2026-09-07, deliberately and as the owner of the files: his standing
     preference is to be handed the full capability and to narrow it afterwards
     if something goes wrong, rather than to keep discovering a missing verb
     mid-task. The bin default is what survived of the old caution, and it is
     enough, because it is reversible.
  2. It still never changes who can see a file. No permissions() call, ever -
     the assistant cannot share Itai's documents with anyone, by mistake or
     otherwise. Deletion is destructive but private and undoable; sharing is
     neither, because a document read by the wrong person cannot be unread.
     This one stays until Itai asks for it by name.
  3. It only writes inside DRIVE_FOLDER_ID. New files are created there, and an
     edit is refused unless the file is already in that folder. Everything
     outside it is readable and not writable.

Rules 2-3 are enforced by a test that reads this file's syntax tree, on the
model of test_module_exposes_no_way_to_send_mail. If a future change needs one
of them relaxed, that test is the conversation - not an obstacle to route
around. Rule 1 had that conversation, and the answer was yes.
"""

import io
import logging
import os

import attachment_readers
import google_scopes

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

# Full drive, for the reason set out above, and everything else the one refresh
# token carries - the whole list lives in google_scopes, because a token is
# minted once for all of it and re-consenting for any single API would
# invalidate access to the rest.
SCOPES = google_scopes.SCOPES

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")

# The one folder the assistant may write into. Without it every write is
# refused - an unset folder means "nowhere", never "anywhere".
FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip()

MAX_RESULTS = 15
MAX_LISTING_CHARS = 4000

# Google's own formats have no bytes to download - they are exported instead.
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "csv"),
    "application/vnd.google-apps.presentation": ("text/plain", "txt"),
}

FOLDER_MIME = "application/vnd.google-apps.folder"

_service = None


def _refresh_token() -> str:
    """One token serves mail and Drive. GOOGLE_REFRESH_TOKEN is the name that
    now describes it; GMAIL_REFRESH_TOKEN is what it is still called in the
    environment, and is accepted so that widening the scopes did not require
    renaming a variable in production at the same moment."""
    return (
        os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
        or os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    )


def _drive_service():
    global _service
    if _service is None:
        token = _refresh_token()
        if not (CLIENT_ID and CLIENT_SECRET and token):
            raise RuntimeError(
                "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN are not set"
            )
        credentials = Credentials(
            token=None,
            refresh_token=token,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        _service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _service


def _escape(value: str) -> str:
    """Drive query literals are single-quoted, so an apostrophe in a filename
    would otherwise end the string early and produce a syntax error from the
    API rather than a search."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _describe(item: dict) -> str:
    kind = "תיקייה" if item.get("mimeType") == FOLDER_MIME else "קובץ"
    owners = item.get("owners") or []
    owner = (owners[0].get("displayName") or owners[0].get("emailAddress")) if owners else ""
    modified = (item.get("modifiedTime") or "")[:10]
    line = f"{kind}: {item.get('name', '(ללא שם)')} [id:{item.get('id', '')}]"
    if owner:
        line += f" | בעלים: {owner}"
    if modified:
        line += f" | עודכן: {modified}"
    if item.get("shared") and not item.get("ownedByMe", True):
        line += " | משותף איתך"
    return line


def _list(query: str, label: str) -> str:
    service = _drive_service()
    result = service.files().list(
        q=query,
        pageSize=MAX_RESULTS,
        orderBy="modifiedTime desc",
        fields="files(id, name, mimeType, modifiedTime, owners(displayName, emailAddress), shared, ownedByMe)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    if not files:
        return f"לא נמצאו קבצים {label}."
    lines = [_describe(f) for f in files]
    out = "\n".join(lines)
    if len(out) > MAX_LISTING_CHARS:
        out = out[:MAX_LISTING_CHARS].rsplit("\n", 1)[0] + "\n[הרשימה קוצרה]"
    return out


def _parents(file_id: str) -> list:
    meta = _drive_service().files().get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    return meta.get("parents") or []


def _refuse_write(reason: str) -> str:
    return f"❌ {reason}"


def search_drive(query: str, shared_with_me_only: bool = False) -> str:
    """Searches Itai's Google Drive by name and by the text inside files.

    Covers his own files and everything other people have shared with him. Use
    it when he mentions a document, a spreadsheet or a presentation without
    giving you a link, and you need to find it before you can read it.

    The result lists each file with an id in square brackets. That id is what
    read_drive_file and update_drive_file take - never guess one.

    Args:
        query: What to look for, e.g. 'תוכנית עבודה ספטמבר'. Matches file names
            and file contents.
        shared_with_me_only: True to search only files other people shared with
            him, which is the fast way to find something a colleague sent.

    Returns:
        A list of matching files with their ids, or a note that nothing matched.
    """
    query = (query or "").strip()
    if not query:
        return "צריך מה לחפש."
    logger.info(f"Drive tool: search_drive(query={query!r}, shared_only={shared_with_me_only})")
    safe = _escape(query)
    clauses = [f"(name contains '{safe}' or fullText contains '{safe}')", "trashed = false"]
    if shared_with_me_only:
        clauses.append("sharedWithMe = true")
    try:
        return _list(" and ".join(clauses), f"על '{query}'")
    except Exception as e:
        logger.error(f"search_drive failed for {query!r}: {e}")
        return f"❌ החיפוש בדרייב נכשל: {e}"


def list_drive_folder() -> str:
    """Lists what is in the assistant's working folder in Drive.

    This is the one folder it can write into, so this is the place to look
    before creating something that may already exist, and the place to find a
    file it made earlier.

    Returns:
        The folder's contents with their ids, or a note that it is empty.
    """
    if not FOLDER_ID:
        return "לא הוגדרה תיקיית עבודה בדרייב (DRIVE_FOLDER_ID). תגיד לאיתי."
    logger.info("Drive tool: list_drive_folder()")
    try:
        return _list(f"'{_escape(FOLDER_ID)}' in parents and trashed = false", "בתיקיית העבודה")
    except Exception as e:
        logger.error(f"list_drive_folder failed: {e}")
        return f"❌ קריאת התיקייה נכשלה: {e}"


def read_drive_file(file_id: str, part: int = 1) -> str:
    """Reads a file from Drive and returns its text.

    Handles Google Docs, Sheets and Slides as well as uploaded xlsx, docx, pdf,
    csv and plain text. Get the id from search_drive or list_drive_folder.

    A long file comes back one part at a time and says so at the end. If what
    Itai asked about is not in part 1, call this again with part=2 rather than
    answering from the first page alone.

    Args:
        file_id: The file's Drive id, as it appeared in square brackets.
        part: Which page of a long file to read. Starts at 1.

    Returns:
        The file's text, or an explanation of why it could not be read.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return "צריך מזהה קובץ."
    logger.info(f"Drive tool: read_drive_file(file_id={file_id!r}, part={part})")
    try:
        service = _drive_service()
        meta = service.files().get(
            fileId=file_id, fields="id, name, mimeType, size", supportsAllDrives=True
        ).execute()
        name = meta.get("name", "file")
        mime = meta.get("mimeType", "")

        if mime == FOLDER_MIME:
            return f"'{name}' היא תיקייה, לא קובץ. אפשר לחפש בתוכה עם search_drive."

        if mime in GOOGLE_EXPORTS:
            export_mime, extension = GOOGLE_EXPORTS[mime]
            raw = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            filename = f"{name}.{extension}"
        else:
            size = int(meta.get("size") or 0)
            if size > attachment_readers.MAX_ATTACHMENT_BYTES:
                mb = attachment_readers.MAX_ATTACHMENT_BYTES / (1024 * 1024)
                return f"'{name}' גדול מדי לקריאה ({size / (1024 * 1024):.1f}MB, המקסימום {mb:.0f}MB)."
            raw = service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
            filename = name

        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return attachment_readers.extract_text(filename, raw, mime_type=mime, part=part)
    except Exception as e:
        logger.error(f"read_drive_file failed for {file_id!r}: {e}")
        return f"❌ קריאת הקובץ נכשלה: {e}"


def create_drive_file(name: str, content: str, file_type: str = "doc") -> str:
    """Creates a new file in the assistant's working folder in Drive.

    Use it when Itai asks for something written down rather than said: a
    summary, a report, a list, a table he wants to keep or share.

    It cannot create a file anywhere else in his Drive, by design. Say where it
    was saved when you report back.

    Args:
        name: The file name, without an extension.
        content: The full text. For file_type='sheet' give CSV - one row per
            line, cells separated by commas.
        file_type: 'doc' for a Google Doc, 'sheet' for a Google Sheet, 'text'
            for a plain text file.

    Returns:
        Confirmation with the new file's id and a link, or the reason it failed.
    """
    name = (name or "").strip()
    content = content or ""
    if not name:
        return _refuse_write("צריך שם לקובץ.")
    if not FOLDER_ID:
        return _refuse_write("לא הוגדרה תיקיית עבודה בדרייב (DRIVE_FOLDER_ID), אז אין לאן לכתוב.")

    targets = {
        "doc": ("application/vnd.google-apps.document", "text/plain"),
        "sheet": ("application/vnd.google-apps.spreadsheet", "text/csv"),
        "text": ("text/plain", "text/plain"),
    }
    if file_type not in targets:
        return _refuse_write(f"סוג קובץ לא מוכר: {file_type}. אפשר doc, sheet או text.")
    target_mime, upload_mime = targets[file_type]

    logger.info(f"Drive tool: create_drive_file(name={name!r}, type={file_type})")
    try:
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype=upload_mime, resumable=False
        )
        created = _drive_service().files().create(
            body={"name": name, "mimeType": target_mime, "parents": [FOLDER_ID]},
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
        link = created.get("webViewLink", "")
        return (
            f"✅ נוצר בדרייב בתיקיית העבודה: {created.get('name')} "
            f"[id:{created.get('id')}]" + (f"\n{link}" if link else "")
        )
    except Exception as e:
        logger.error(f"create_drive_file failed for {name!r}: {e}")
        return f"❌ יצירת הקובץ נכשלה: {e}"


def update_drive_file(file_id: str, content: str) -> str:
    """Replaces the contents of a file the assistant may edit.

    Only files inside the working folder can be edited. Anything else in Itai's
    Drive - including files other people shared with him - is readable and not
    writable, and an attempt says so rather than failing obscurely.

    This REPLACES the whole file. To add to a document, read it first with
    read_drive_file and send back the old text plus the new.

    Args:
        file_id: The file's Drive id.
        content: The complete new contents.

    Returns:
        Confirmation, or the reason the edit was refused.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return _refuse_write("צריך מזהה קובץ.")
    if not FOLDER_ID:
        return _refuse_write("לא הוגדרה תיקיית עבודה בדרייב (DRIVE_FOLDER_ID), אז אין מה לערוך.")

    logger.info(f"Drive tool: update_drive_file(file_id={file_id!r})")
    try:
        if FOLDER_ID not in _parents(file_id):
            return _refuse_write(
                "הקובץ הזה לא נמצא בתיקיית העבודה, ולכן אני יכול רק לקרוא אותו ולא לשנות אותו. "
                "אם צריך לערוך אותו — אפשר שאיצור עותק חדש בתיקיית העבודה."
            )
        service = _drive_service()
        meta = service.files().get(fileId=file_id, fields="mimeType, name", supportsAllDrives=True).execute()
        mime = meta.get("mimeType", "")
        upload_mime = "text/csv" if mime == "application/vnd.google-apps.spreadsheet" else "text/plain"
        media = MediaIoBaseUpload(
            io.BytesIO((content or "").encode("utf-8")), mimetype=upload_mime, resumable=False
        )
        updated = service.files().update(
            fileId=file_id, media_body=media, fields="id, name", supportsAllDrives=True
        ).execute()
        return f"✅ הקובץ עודכן: {updated.get('name')} [id:{updated.get('id')}]"
    except Exception as e:
        logger.error(f"update_drive_file failed for {file_id!r}: {e}")
        return f"❌ עדכון הקובץ נכשל: {e}"


def trash_drive_file(file_id: str, permanent: bool = False) -> str:
    """Moves a file in Itai's Drive to the bin, or deletes it for good.

    The default puts the file in the Drive bin, where it stays recoverable for
    30 days and Itai can restore it himself. Use permanent=True only when he
    has said in this conversation that he wants it gone for good - that one has
    no undo, so confirm it with him in words before calling it that way.

    Unlike editing, this is not restricted to the working folder: it can bin
    anything Itai owns anywhere in his Drive. A file somebody else owns cannot
    be binned by him at all, and Google's refusal is reported as it comes.

    Args:
        file_id: The file's Drive id, as it comes back from search_drive.
        permanent: True to delete for good instead of binning. Ask first.

    Returns:
        Confirmation naming the file, or the reason Google refused.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return _refuse_write("צריך מזהה קובץ.")

    logger.info(f"Drive tool: trash_drive_file(file_id={file_id!r}, permanent={permanent})")
    try:
        service = _drive_service()
        # Read the name before acting: the confirmation has to say what actually
        # went, and after a permanent delete there is nothing left to ask.
        meta = service.files().get(
            fileId=file_id, fields="name, ownedByMe, trashed", supportsAllDrives=True
        ).execute()
        name = meta.get("name", "(ללא שם)")
        if meta.get("trashed") and not permanent:
            return f"ℹ️ הקובץ {name!r} כבר נמצא בפח של הדרייב."
        if permanent:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            return f"🗑️ {name!r} נמחק לצמיתות. אין דרך לשחזר אותו."
        service.files().update(
            fileId=file_id, body={"trashed": True}, fields="id", supportsAllDrives=True
        ).execute()
        return (
            f"🗑️ {name!r} הועבר לפח בדרייב, וניתן לשחזור משם במשך 30 יום."
        )
    except Exception as e:
        logger.error(f"trash_drive_file failed for {file_id!r}: {e}")
        return f"❌ לא הצלחתי למחוק את הקובץ: {e}"
