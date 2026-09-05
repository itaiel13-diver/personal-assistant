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
    return f"{when} — {title}" + (f" @ {location}" if location else "")


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


def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    location: str = "",
    description: str = "",
) -> str:
    """Creates an event in Itai's Google Calendar.
    start_time and end_time must be ISO 8601 local Israel time without a timezone
    suffix, for example '2026-09-06T09:30:00'.
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
        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return f"✅ נקבע ביומן: {title} ב-{start_time}. קישור: {created.get('htmlLink', '')}"
    except Exception as e:
        logger.error(f"Calendar write failed: {e}")
        return f"❌ שגיאה בקביעת האירוע: {e}"
