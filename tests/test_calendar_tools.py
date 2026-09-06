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


def test_listed_events_include_their_id_so_they_can_be_targeted():
    """update/delete need the event id, and the model can only get it from the listing."""
    items = [{"summary": "ביקור", "id": "evt-123", "start": {"dateTime": "2026-09-06T09:00:00+03:00"}}]
    with patch.object(calendar_tools, "_calendar_service", return_value=_service_with_events(items)):
        result = calendar_tools.get_calendar_events()
    assert "[id:evt-123]" in result


def test_update_only_patches_the_fields_that_were_given():
    """Empty arguments must not blank out existing values on the event."""
    service = MagicMock()
    service.events.return_value.patch.return_value.execute.return_value = {"summary": "חדש"}
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        calendar_tools.update_calendar_event("evt-123", title="חדש", colour_id="10")
    body = service.events.return_value.patch.call_args.kwargs["body"]
    assert body == {"summary": "חדש", "colorId": "10"}
    assert "start" not in body and "location" not in body


def test_update_sends_israel_timezone_when_times_change():
    service = MagicMock()
    service.events.return_value.patch.return_value.execute.return_value = {"summary": "x"}
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        calendar_tools.update_calendar_event(
            "evt-123", start_time="2026-09-06T11:00:00", end_time="2026-09-06T12:00:00"
        )
    body = service.events.return_value.patch.call_args.kwargs["body"]
    assert body["start"]["timeZone"] == "Asia/Jerusalem"
    assert body["end"]["timeZone"] == "Asia/Jerusalem"


def test_update_with_no_fields_does_not_call_the_api():
    service = MagicMock()
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        result = calendar_tools.update_calendar_event("evt-123")
    service.events.return_value.patch.assert_not_called()
    assert "לא צוין" in result


def test_delete_names_the_event_it_removed():
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = {"summary": "ביקור רמלה"}
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        result = calendar_tools.delete_calendar_event("evt-123")
    service.events.return_value.delete.assert_called_once()
    assert "ביקור רמלה" in result


def test_delete_returns_error_string_instead_of_raising():
    service = MagicMock()
    service.events.return_value.delete.return_value.execute.side_effect = RuntimeError("boom")
    with patch.object(calendar_tools, "_calendar_service", return_value=service):
        result = calendar_tools.delete_calendar_event("evt-123")
    assert result.startswith("❌")
