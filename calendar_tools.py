import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# The service account has its own (empty) "primary" calendar, so the target
# calendar must be named explicitly - it is Itai's calendar, shared with the
# service account, and its ID is his Google account address.
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

_service = None


def _calendar_service():
    global _service
    if _service is None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=SCOPES
        )
        _service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return _service


def _format_event(event: dict) -> str:
    start = event.get("start", {})
    # All-day events carry "date"; timed events carry "dateTime".
    when = start.get("dateTime") or start.get("date", "")
    title = event.get("summary", "(ללא כותרת)")
    location = event.get("location", "")
    # The id is included so the model can target this exact event when Itai
    # asks to move, recolour or delete it.
    return (
        f"{when} — {title}"
        + (f" @ {location}" if location else "")
        + f" [id:{event.get('id', '')}]"
    )


def get_calendar_events(days_ahead: int = 1) -> str:
    """Reads Itai's Google Calendar and lists his events from now until N days ahead.
    Use this before proposing a daily schedule, a driving route, or checking availability.
    days_ahead=1 covers the next 24 hours, 7 covers the coming week."""
    try:
        service = _calendar_service()
        now = datetime.now(ISRAEL_TZ)
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"אין אירועים ביומן ב-{days_ahead} הימים הקרובים."
        return "\n".join(_format_event(e) for e in events)
    except Exception as e:
        logger.error(f"Calendar read failed: {e}")
        return f"❌ שגיאה בקריאת היומן: {e}"


# Verified against the live API (colors().get()) rather than assumed - these are
# the only eleven values Google accepts for an event's colorId.
COLOR_PALETTE = """colour_id options: 1=light blue-purple, 2=mint green, 3=purple,
4=salmon pink, 5=yellow, 6=orange, 7=cyan, 8=grey, 9=blue, 10=green, 11=red"""


def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    location: str = "",
    description: str = "",
    colour_id: str = "",
) -> str:
    """Creates an event in Itai's Google Calendar.
    start_time and end_time must be ISO 8601 local Israel time without a timezone
    suffix, for example '2026-09-06T09:30:00'.
    colour_id is optional. 1=light blue-purple, 2=mint green, 3=purple, 4=salmon pink,
    5=yellow, 6=orange, 7=cyan, 8=grey, 9=blue, 10=green, 11=red.
    Always confirm the exact details with Itai before calling this - never invent
    a time he did not approve."""
    try:
        service = _calendar_service()
        event = {
            "summary": title,
            "location": location,
            "description": description,
            "start": {"dateTime": start_time, "timeZone": "Asia/Jerusalem"},
            "end": {"dateTime": end_time, "timeZone": "Asia/Jerusalem"},
        }
        if colour_id:
            event["colorId"] = colour_id
        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return (
            f"✅ נקבע ביומן: {title} ב-{start_time} "
            f"[id:{created.get('id', '')}]. קישור: {created.get('htmlLink', '')}"
        )
    except Exception as e:
        logger.error(f"Calendar write failed: {e}")
        return f"❌ שגיאה בקביעת האירוע: {e}"


def update_calendar_event(
    event_id: str,
    title: str = "",
    start_time: str = "",
    end_time: str = "",
    location: str = "",
    description: str = "",
    colour_id: str = "",
) -> str:
    """Changes an existing event in Itai's Google Calendar. Get event_id from
    get_calendar_events, which prints it as [id:...] after each event.
    Only the fields you pass are changed; everything left empty stays as it is.
    Use this to move an event to a different time, rename it, change its location,
    or recolour it. colour_id: 1=light blue-purple, 2=mint green, 3=purple,
    4=salmon pink, 5=yellow, 6=orange, 7=cyan, 8=grey, 9=blue, 10=green, 11=red.
    If start_time is changed, end_time should normally be changed too.
    Confirm with Itai before changing anything he did not explicitly ask to change."""
    try:
        service = _calendar_service()
        patch: dict = {}
        if title:
            patch["summary"] = title
        if location:
            patch["location"] = location
        if description:
            patch["description"] = description
        if colour_id:
            patch["colorId"] = colour_id
        if start_time:
            patch["start"] = {"dateTime": start_time, "timeZone": "Asia/Jerusalem"}
        if end_time:
            patch["end"] = {"dateTime": end_time, "timeZone": "Asia/Jerusalem"}
        if not patch:
            return "לא צוין שום שדה לעדכון."
        updated = service.events().patch(
            calendarId=CALENDAR_ID, eventId=event_id, body=patch
        ).execute()
        return f"✅ עודכן: {updated.get('summary', '')} — שדות ששונו: {', '.join(patch.keys())}"
    except Exception as e:
        logger.error(f"Calendar update failed: {e}")
        return f"❌ שגיאה בעדכון האירוע: {e}"


def delete_calendar_event(event_id: str) -> str:
    """Deletes an event from Itai's Google Calendar permanently. Get event_id from
    get_calendar_events, which prints it as [id:...] after each event.
    This cannot be undone - ALWAYS state which event you are about to delete and get
    Itai's explicit confirmation first. Never delete an event he did not name."""
    try:
        service = _calendar_service()
        # Read it first so the confirmation message names what actually went.
        try:
            existing = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
            title = existing.get("summary", "(ללא כותרת)")
        except Exception:
            title = "(לא נמצאה כותרת)"
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return f"🗑️ נמחק מהיומן: {title}"
    except Exception as e:
        logger.error(f"Calendar delete failed: {e}")
        return f"❌ שגיאה במחיקת האירוע: {e}"
