from unittest.mock import MagicMock, patch

import calendar_tools


def _service_with_events(items):
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": items}
    return service


def test_get_calendar_events_formats_timed_and_all_day_events():
    items = [
        {"summary": "ביקור רחובות", "location": "קניון עופר", "start": {"dateTime": "2026-09-06T09:00:00+03:00"}},
        {"summary": "יום חופש", "start": {"date": "2026-09-07"}},  # all-day events use "date", not "dateTime"
    ]
    with patch.object(calendar_tools, "_calendar_service", return_value=_service_with_events(items)):
        result = calendar_tools.get_calendar_events(days_ahead=7)
    assert "ביקור רחובות" in result
    assert "קניון עופר" in result
    assert "יום חופש" in result
    assert "2026-09-07" in result


def test_get_calendar_events_reports_empty_calendar_clearly():
    with patch.object(calendar_tools, "_calendar_service", return_value=_service_with_events([])):
        result = calendar_tools.get_calendar_events(days_ahead=3)
    assert "אין אירועים" in result


def test_get_calendar_events_returns_error_string_instead_of_raising():
    """Tool functions are called by Gemini - an exception would break the whole
    conversation turn, so failures must come back as readable text."""
    with patch.object(calendar_tools, "_calendar_service", side_effect=RuntimeError("boom")):
        result = calendar_tools.get_calendar_events()
    assert result.startswith("❌")


def test_create_calendar_event_sends_israel_timezone():
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {"htmlLink": "http://example/e"}
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        result = calendar_tools.create_calendar_event(
            "ביקור KSP", "2026-09-06T09:30:00", "2026-09-06T10:30:00", location="קרית עקרון"
        )
    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"]["timeZone"] == "Asia/Jerusalem"
    assert body["end"]["timeZone"] == "Asia/Jerusalem"
    assert body["summary"] == "ביקור KSP"
    assert body["location"] == "קרית עקרון"
    assert "✅" in result


def test_create_calendar_event_returns_error_string_instead_of_raising():
    with patch.object(calendar_tools, "_calendar_service", side_effect=RuntimeError("boom")):
        result = calendar_tools.create_calendar_event("x", "2026-09-06T09:00:00", "2026-09-06T10:00:00")
    assert result.startswith("❌")
