import ast
import base64
from unittest.mock import MagicMock, patch

import attachment_readers
import gmail_tools


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def test_module_exposes_no_way_to_send_mail():
    """gmail.compose DOES permit sending - verified against the live API, where
    drafts().send() succeeded and actually delivered a message. So the guarantee
    that the assistant never sends mail rests entirely on this module offering no
    sending function and never calling drafts().send() or messages().send().
    If that ever changes, this test is the thing that should stop it."""
    # Parsed rather than grepped, so comments and docstrings that merely discuss
    # sending do not trip it - only a real call does.
    tree = ast.parse(open(gmail_tools.__file__, encoding="utf-8").read())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "send" not in called, "gmail_tools now calls send() somewhere"
    public = [n for n in dir(gmail_tools) if not n.startswith("_") and callable(getattr(gmail_tools, n))]
    assert not any("send" in n.lower() for n in public), f"a sending function is exposed: {public}"


def test_scopes_are_limited_to_read_and_compose():
    """Broader scopes (gmail.modify, full mail.google.com) would also allow
    deleting mail and changing labels, which the assistant has no reason to do."""
    assert sorted(gmail_tools.SCOPES) == sorted([
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ])


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


def _xlsx_bytes(rows: list) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.title = "סטטוס"
    for row in rows:
        workbook.active.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _message_with_attachment(filename, mime_type, size, attachment_id="att1"):
    """The shape Gmail actually returns: the attachment part carries a filename
    and a body with an attachmentId but no data, which is exactly why walking the
    tree for text alone misses it."""
    return {
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Fwd: קובץ סטטוס הדרכות"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("מצורף הקובץ")}},
                {
                    "mimeType": mime_type,
                    "filename": filename,
                    "body": {"attachmentId": attachment_id, "size": size},
                },
            ],
        }
    }


def test_read_email_lists_attachments_that_the_body_walk_would_have_dropped():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        _message_with_attachment("Z8 Training Status.xlsx", "application/octet-stream", 34000)
    )
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email("m1")
    assert "Z8 Training Status.xlsx" in result
    assert "ניתן לקריאה" in result
    assert "read_email_attachment" in result, "the model is not told how to open it"


def test_read_email_marks_an_unreadable_attachment_as_such():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        _message_with_attachment("scan.jpg", "image/jpeg", 900_000)
    )
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email("m1")
    assert "לא ניתן לקריאה" in result


def test_read_attachment_downloads_and_converts_a_real_xlsx():
    raw = _xlsx_bytes([["חנות", "סטטוס"], ["רחובות", "בוצע"]])
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = _message_with_attachment(
        "Z8 Training Status.xlsx", "application/octet-stream", len(raw)
    )
    messages.attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(raw).decode()
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email_attachment("m1", "Z8 Training Status.xlsx")
    assert "רחובות" in result and "בוצע" in result
    messages.attachments.return_value.get.assert_called_once_with(
        userId="me", messageId="m1", id="att1"
    )


def test_read_attachment_matches_on_a_partial_name():
    """The model rarely reproduces a long filename exactly; requiring a byte-for-byte
    match would make the tool unusable in practice."""
    raw = _xlsx_bytes([["ערך"]])
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = _message_with_attachment(
        "Z8 Training Status.xlsx", "application/octet-stream", len(raw)
    )
    messages.attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(raw).decode()
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        assert "ערך" in gmail_tools.read_email_attachment("m1", "training status")


def test_read_attachment_refuses_an_ambiguous_name_instead_of_guessing():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {"parts": [
            {"filename": "דוח ינואר.xlsx", "mimeType": "application/vnd.ms-excel",
             "body": {"attachmentId": "a1", "size": 100}},
            {"filename": "דוח פברואר.xlsx", "mimeType": "application/vnd.ms-excel",
             "body": {"attachmentId": "a2", "size": 100}},
        ]}
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email_attachment("m1", "דוח")
    assert result.startswith("❌")
    assert "ינואר" in result and "פברואר" in result
    service.users.return_value.messages.return_value.attachments.assert_not_called()


def test_read_attachment_names_what_is_there_when_the_name_is_wrong():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        _message_with_attachment("Z8 Training Status.xlsx", "application/octet-stream", 100)
    )
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email_attachment("m1", "budget.xlsx")
    assert "Z8 Training Status.xlsx" in result


def test_read_attachment_skips_the_download_for_an_unreadable_type():
    """Checked before the round trip - there is no point spending the API call and
    the memory on bytes that cannot become text anyway."""
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        _message_with_attachment("photo.jpg", "image/jpeg", 500_000)
    )
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email_attachment("m1", "photo.jpg")
    assert result.startswith("❌") and "תמונה" in result
    service.users.return_value.messages.return_value.attachments.assert_not_called()


def test_read_attachment_refuses_an_oversized_file_before_downloading_it():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = (
        _message_with_attachment("huge.xlsx", "application/octet-stream",
                                 attachment_readers.MAX_ATTACHMENT_BYTES + 1)
    )
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        result = gmail_tools.read_email_attachment("m1", "huge.xlsx")
    assert result.startswith("❌") and "גדול" in result
    service.users.return_value.messages.return_value.attachments.assert_not_called()


def test_read_attachment_says_so_when_there_are_none():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {"mimeType": "text/plain", "body": {"data": _b64("טקסט")}}
    }
    with patch.object(gmail_tools, "_gmail_service", return_value=service):
        assert "אין קבצים מצורפים" in gmail_tools.read_email_attachment("m1", "x.xlsx")


def test_list_attachments_finds_one_nested_deep_in_the_part_tree():
    payload = {"parts": [{"parts": [{"parts": [
        {"filename": "עמוק.pdf", "mimeType": "application/pdf",
         "body": {"attachmentId": "deep", "size": 10}},
    ]}]}]}
    found = gmail_tools._list_attachments(payload)
    assert [a["filename"] for a in found] == ["עמוק.pdf"]


def test_inline_images_without_an_attachment_id_are_not_listed():
    """Signature logos arrive as parts with a filename but inline data; listing them
    would bury a real attachment in noise."""
    payload = {"parts": [
        {"filename": "logo.png", "mimeType": "image/png", "body": {"data": _b64("x")}},
    ]}
    assert gmail_tools._list_attachments(payload) == []


def test_read_attachment_returns_an_error_string_instead_of_raising():
    with patch.object(gmail_tools, "_gmail_service", side_effect=RuntimeError("boom")):
        assert gmail_tools.read_email_attachment("m1", "a.xlsx").startswith("❌")
