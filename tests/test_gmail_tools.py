import base64
from unittest.mock import MagicMock, patch

import gmail_tools


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def test_scopes_cannot_send_mail():
    """The whole safety design rests on this: compose creates drafts, send sends.
    If a send scope ever appears here, the assistant can email people by itself."""
    assert not any("gmail.send" in s or "mail.google.com" in s for s in gmail_tools.SCOPES)
    assert any(s.endswith("gmail.compose") for s in gmail_tools.SCOPES)


def test_extract_body_finds_plain_text_nested_in_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("שלום איתי")}},
            ]},
        ],
    }
    assert gmail_tools._extract_body(payload) == "שלום איתי"


def test_extract_body_falls_back_to_html_when_no_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/html", "body": {"data": _b64("<p>hi</p>")}}],
    }
    assert "hi" in gmail_tools._extract_body(payload)


def test_extract_body_survives_an_empty_payload():
    assert gmail_tools._extract_body({}) == ""


def test_search_emails_lists_ids_so_they_can_be_read():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "From", "value": "samsung@example.com"},
            {"name": "Subject", "value": "יעדים"},
            {"name": "Date", "value": "Sun, 6 Sep 2026"},
        ]}
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.search_emails("is:unread")
    assert "[id:m1]" in result
    assert "יעדים" in result


def test_search_emails_reports_no_results_clearly():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.search_emails("from:nobody")
    assert "לא נמצאו" in result


def test_read_email_truncates_very_long_bodies():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("x" * 9000)},
            "headers": [{"name": "Subject", "value": "ארוך"}],
        }
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email("m1")
    assert "נחתכה" in result
    assert len(result) < 6000


def test_create_draft_uses_the_drafts_endpoint_not_send():
    service = MagicMock()
    service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.create_email_draft("a@b.com", "נושא", "תוכן")
    service.users.return_value.drafts.return_value.create.assert_called_once()
    service.users.return_value.messages.return_value.send.assert_not_called()
    assert "לא נשלחה" in result


def test_create_draft_encodes_hebrew_without_mangling_it():
    service = MagicMock()
    service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        gmail_tools.create_email_draft("a@b.com", "דוח חודשי", "שלום, מצורף הדוח")
    raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    assert "דוח חודשי" in decoded or "=?utf-8?" in decoded.lower()


def test_tools_return_error_strings_instead_of_raising():
    with patch.object(gmail_tools, "_gmail_service", side_effect=RuntimeError("boom")):
        assert gmail_tools.search_emails().startswith("❌")
        assert gmail_tools.read_email("m1").startswith("❌")
        assert gmail_tools.create_email_draft("a@b.com", "s", "b").startswith("❌")
