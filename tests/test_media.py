"""Images and voice notes: the download from Meta, the webhook routing, and
the two assistant entry points. Everything external is mocked - no Meta, no
Gemini - so a failure here means the wiring broke, not the network."""
import json
from unittest.mock import MagicMock, patch

import pytest

import assistant
import media_tools
import webhook_server


# --- helpers -------------------------------------------------------------


def _sign(raw_body: bytes) -> str:
    import hashlib
    import hmac
    digest = hmac.new(webhook_server.META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _media_payload(sender: str, kind: str, mime: str, caption: str = None) -> dict:
    block = {"id": "media-id-1", "mime_type": mime}
    if caption is not None:
        block["caption"] = caption
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry-id",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "123"},
                    "messages": [{
                        "from": sender,
                        "id": "wamid.test",
                        "timestamp": "1234567890",
                        kind: block,
                        "type": kind,
                    }],
                },
                "field": "messages",
            }],
        }],
    }


@pytest.fixture
def client():
    webhook_server.app.testing = True
    return webhook_server.app.test_client()


# --- media_tools.download_media ------------------------------------------


def _meta_lookup_response(url="https://lookaside.fbsbx.com/x", mime="image/jpeg", size=1234):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"url": url, "mime_type": mime, "file_size": size}
    return r


def _download_response(content=b"\xff\xd8\xff-bytes"):
    r = MagicMock()
    r.status_code = 200
    r.iter_content.return_value = [content]
    return r


def test_download_media_resolves_the_id_then_fetches_the_url(monkeypatch):
    """Meta's flow is two calls: id -> URL, URL -> bytes. Both carry the same
    bearer token as outgoing messages."""
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    with patch("media_tools.requests.get") as mock_get:
        mock_get.side_effect = [_meta_lookup_response(), _download_response()]
        content, mime = media_tools.download_media("media-id-1")

    assert content == b"\xff\xd8\xff-bytes"
    assert mime == "image/jpeg"
    lookup, fetch = mock_get.call_args_list
    assert "media-id-1" in lookup.args[0]
    assert lookup.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert fetch.args[0] == "https://lookaside.fbsbx.com/x"
    assert fetch.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_download_media_returns_none_when_the_lookup_fails(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    failed = MagicMock(status_code=404, text="not found")
    with patch("media_tools.requests.get", return_value=failed):
        assert media_tools.download_media("gone") == (None, None)


def test_download_media_returns_none_when_the_fetch_fails(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    with patch("media_tools.requests.get") as mock_get:
        mock_get.side_effect = [_meta_lookup_response(), MagicMock(status_code=500)]
        assert media_tools.download_media("media-id-1") == (None, None)


def test_download_media_refuses_oversize_media_without_downloading(monkeypatch):
    """A file over Gemini's inline ceiling is not a photo or a voice note -
    and downloading it anyway would only spend memory to discover that."""
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    huge = _meta_lookup_response(size=media_tools.MAX_MEDIA_BYTES + 1)
    with patch("media_tools.requests.get") as mock_get:
        mock_get.side_effect = [huge]
        assert media_tools.download_media("media-id-1") == (None, None)
    assert mock_get.call_count == 1, "the bytes of an oversize file must never be fetched"


def test_download_media_never_raises_on_network_error(monkeypatch):
    import requests
    monkeypatch.setenv("WHATSAPP_TOKEN", "test-token")
    with patch("media_tools.requests.get", side_effect=requests.ConnectionError("boom")):
        assert media_tools.download_media("media-id-1") == (None, None)


def test_base_mime_drops_parameters():
    # WhatsApp voice notes arrive as 'audio/ogg; codecs=opus' - legal HTTP,
    # but Gemini rejects a mime_type with parameters.
    assert media_tools.base_mime("audio/ogg; codecs=opus") == "audio/ogg"
    assert media_tools.base_mime("image/jpeg") == "image/jpeg"
    assert media_tools.base_mime("") == ""


# --- webhook routing -------------------------------------------------------


def test_an_image_is_downloaded_and_answered_with_its_caption(client):
    raw = json.dumps(_media_payload("972500000000", "image", "image/jpeg",
                                    caption="מה רשום על התגית?")).encode()
    with patch("webhook_server.media_tools.download_media",
               return_value=(b"img-bytes", "image/jpeg")) as mock_dl, \
         patch("webhook_server.handle_image_message", return_value="על התגית...") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_dl.assert_called_once_with("media-id-1")
    mock_handle.assert_called_once_with(b"img-bytes", "image/jpeg",
                                        "מה רשום על התגית?", sender_id="972500000000")
    mock_send.assert_called_once_with("972500000000", "על התגית...")


def test_a_voice_note_is_downloaded_and_transcribed(client):
    raw = json.dumps(_media_payload("972500000000", "audio",
                                    "audio/ogg; codecs=opus")).encode()
    with patch("webhook_server.media_tools.download_media",
               return_value=(b"ogg-bytes", "audio/ogg")) as mock_dl, \
         patch("webhook_server.handle_voice_message", return_value="ברוך הבא") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_dl.assert_called_once_with("media-id-1")
    mock_handle.assert_called_once_with(b"ogg-bytes", "audio/ogg", sender_id="972500000000")
    mock_send.assert_called_once_with("972500000000", "ברוך הבא")


def test_a_failed_media_download_still_gets_a_reply(client):
    """Silence looks identical to the bot ignoring him - a failed download
    answers with something he can act on, not with nothing."""
    raw = json.dumps(_media_payload("972500000000", "image", "image/jpeg")).encode()
    with patch("webhook_server.media_tools.download_media", return_value=(None, None)), \
         patch("webhook_server.handle_image_message") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_handle.assert_not_called()
    mock_send.assert_called_once()
    assert "שוב" in mock_send.call_args[0][1]


def test_a_media_message_still_reopens_the_free_window(client):
    raw = json.dumps(_media_payload("972500000000", "audio", "audio/ogg")).encode()
    with patch("webhook_server.media_tools.download_media", return_value=(b"x", "audio/ogg")), \
         patch("webhook_server.handle_voice_message", return_value="ok"), \
         patch("webhook_server._send_whatsapp_reply"), \
         patch.object(webhook_server.storage, "note_inbound") as note:
        client.post("/webhook", data=raw, content_type="application/json",
                    headers={"X-Hub-Signature-256": _sign(raw)})
    note.assert_called_once_with("972500000000")


# --- assistant: voice ------------------------------------------------------


@pytest.fixture
def fake_client(monkeypatch):
    """Swaps the Gemini client for a mock, the boundary assistant.py owns."""
    client_mock = MagicMock()
    monkeypatch.setattr(assistant, "client", client_mock)
    return client_mock


def test_voice_note_is_transcribed_then_answered_as_text(fake_client, monkeypatch):
    """The transcript - not the audio - is what enters the conversation, so
    the stored history stays all-text and the usual reply path answers it."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: False)
    assistant._fallback_sessions.clear()

    fake_client.models.generate_content.return_value = MagicMock(text="תקבע לי פגישה מחר בחמש")
    chat = MagicMock()
    chat.send_message.return_value = MagicMock(text="נרשם")
    fake_client.chats.create.return_value = chat

    reply = assistant.handle_voice_message(b"ogg-bytes", "audio/ogg; codecs=opus",
                                           sender_id="sender-v")

    assert reply == "נרשם"
    # The transcription call carried the audio with a cleaned mime type.
    contents = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert contents[0].inline_data.data == b"ogg-bytes"
    assert contents[0].inline_data.mime_type == "audio/ogg"
    # The conversation received the transcript, marked as a voice note.
    sent = chat.send_message.call_args.args[0]
    assert "הודעה קולית" in sent
    assert "תקבע לי פגישה מחר בחמש" in sent
    assistant._fallback_sessions.clear()


def test_an_unhearable_voice_note_asks_for_a_resend(fake_client, monkeypatch):
    """An empty transcript must not become an answer to words nobody said."""
    fake_client.models.generate_content.return_value = MagicMock(text="")
    reply = assistant.handle_voice_message(b"ogg-bytes", "audio/ogg", sender_id="sender-v")
    assert "שוב" in reply
    fake_client.chats.create.assert_not_called()


def test_a_failed_transcription_never_raises(fake_client):
    fake_client.models.generate_content.side_effect = RuntimeError("boom")
    reply = assistant.handle_voice_message(b"ogg-bytes", "audio/ogg", sender_id="sender-v")
    assert "שוב" in reply


# --- assistant: images -----------------------------------------------------


def test_an_image_goes_to_the_model_inline_with_its_caption(fake_client, monkeypatch):
    monkeypatch.setattr(assistant.storage, "enabled", lambda: False)
    assistant._fallback_sessions.clear()

    chat = MagicMock()
    chat.send_message.return_value = MagicMock(text="זו תצוגת גלקסי")
    fake_client.chats.create.return_value = chat

    reply = assistant.handle_image_message(b"img-bytes", "image/jpeg",
                                           "מה לא בסדר כאן?", sender_id="sender-i")

    assert reply == "זו תצוגת גלקסי"
    parts = chat.send_message.call_args.args[0]
    assert parts[0].inline_data.data == b"img-bytes"
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[1].text == "מה לא בסדר כאן?"
    assistant._fallback_sessions.clear()


def test_an_image_without_a_caption_gets_a_neutral_look_at_this(fake_client, monkeypatch):
    monkeypatch.setattr(assistant.storage, "enabled", lambda: False)
    assistant._fallback_sessions.clear()

    chat = MagicMock()
    chat.send_message.return_value = MagicMock(text="תמונה של מדף")
    fake_client.chats.create.return_value = chat

    assistant.handle_image_message(b"img-bytes", "image/jpeg", "", sender_id="sender-i")
    parts = chat.send_message.call_args.args[0]
    assert parts[1].text, "a captionless photo still needs a text part - the model answers text, not pixels alone"
    assistant._fallback_sessions.clear()


def test_stored_history_carries_a_marker_instead_of_the_image_bytes(fake_client, monkeypatch):
    """The bytes belong in the model's context for one turn, not in Postgres
    forever. What stays is a marker saying a photo was sent."""
    saved = {}
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "save_history",
                        lambda sender, history: saved.update(sender=sender, history=history))
    monkeypatch.setattr(assistant.storage, "load_history", lambda sender: [])

    chat = MagicMock()
    chat.send_message.return_value = MagicMock(text="נראה טוב")
    chat.get_history.return_value = [
        assistant.types.Content(role="user", parts=[
            assistant.types.Part.from_bytes(data=b"img-bytes", mime_type="image/jpeg"),
            assistant.types.Part.from_text(text="מה זה?"),
        ]),
        assistant.types.Content(role="model", parts=[
            assistant.types.Part.from_text(text="נראה טוב"),
        ]),
    ]
    fake_client.chats.create.return_value = chat

    assistant.handle_image_message(b"img-bytes", "image/jpeg", "מה זה?", sender_id="sender-i")

    stored = json.dumps(saved["history"], ensure_ascii=False)
    assert "aW1n" not in stored and "inline_data" not in stored, "image bytes leaked into stored history"
    assert "תמונה" in stored, "the placeholder marker is missing"
    assert "מה זה?" in stored and "נראה טוב" in stored


def test_an_image_after_the_daily_quota_goes_to_a_vision_fallback(fake_client, monkeypatch):
    """Gemini's 429 is no longer the end of a photo: a spare tier's vision
    model sees the same bytes and answers, and the turn is still recorded."""
    saved = []
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "append_user_turn",
                        lambda sender, text: saved.append(("user", text)))
    monkeypatch.setattr(assistant.storage, "append_model_turn",
                        lambda sender, text: saved.append(("model", text)))
    monkeypatch.setattr(assistant.storage, "load_history", lambda sender: [])
    assistant._fallback_sessions.clear()

    quota_error = assistant.genai_errors.ClientError(429, {"error": {"message": "quota"}})
    chat = MagicMock()
    chat.send_message.side_effect = quota_error
    fake_client.chats.create.return_value = chat

    seen = {}
    def fake_ask_image(image_bytes, mime_type, prompt, system="", skip=()):
        seen.update(bytes=image_bytes, mime=mime_type, skip=skip)
        return "זו תצוגת גלקסי בחנות"
    monkeypatch.setattr(assistant.llm, "ask_image", fake_ask_image)

    reply = assistant.handle_image_message(b"img-bytes", "image/jpeg", "מה זה?", sender_id="sender-i")

    assert reply == "זו תצוגת גלקסי בחנות"
    assert seen["bytes"] == b"img-bytes" and seen["mime"] == "image/jpeg"
    assert seen["skip"] == ("gemini",), "Gemini already said 429 - asking it again wastes a round trip"
    assert saved[0][0] == "user" and "תמונה" in saved[0][1] and "מה זה?" in saved[0][1]
    assert saved[1] == ("model", "זו תצוגת גלקסי בחנות")
    assistant._fallback_sessions.clear()


def test_an_image_with_no_vision_tier_left_gets_an_honest_answer(fake_client, monkeypatch):
    """Every vision provider refusing still ends honestly - never a blind
    'description', which would be the worst available failure."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: False)
    monkeypatch.setattr(assistant.llm, "ask_image", lambda *a, **k: None)
    assistant._fallback_sessions.clear()

    quota_error = assistant.genai_errors.ClientError(429, {"error": {"message": "quota"}})
    chat = MagicMock()
    chat.send_message.side_effect = quota_error
    fake_client.chats.create.return_value = chat

    reply = assistant.handle_image_message(b"img-bytes", "image/jpeg", "", sender_id="sender-i")
    assert "מכסת" in reply
    assert "מודל הגיבוי" in reply
    assistant._fallback_sessions.clear()


# --- WhatsApp documents ----------------------------------------------------


def _document_payload(sender: str, mime: str, filename: str, caption: str = None) -> dict:
    payload = _media_payload(sender, "document", mime, caption)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["document"]["filename"] = filename
    return payload


def test_a_document_is_downloaded_and_read_into_the_conversation(client):
    raw = json.dumps(_document_payload("972500000000", "application/pdf",
                                       "דוח.pdf", caption="מה מסקנות?")).encode()
    with patch("webhook_server.media_tools.download_media",
               return_value=(b"pdf-bytes", "application/pdf")), \
         patch("webhook_server.handle_document_message", return_value="המסקנות...") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_handle.assert_called_once_with(b"pdf-bytes", "דוח.pdf", "application/pdf",
                                        "מה מסקנות?", sender_id="972500000000")
    mock_send.assert_called_once_with("972500000000", "המסקנות...")


def test_a_photo_sent_as_a_document_is_seen_as_a_photo(client):
    """WhatsApp compresses photos; sending one 'as a document' keeps the
    original - the bytes are an image whichever button he pressed."""
    raw = json.dumps(_document_payload("972500000000", "image/png", "disp.png")).encode()
    with patch("webhook_server.media_tools.download_media",
               return_value=(b"png-bytes", "image/png")), \
         patch("webhook_server.handle_image_message", return_value="רואה") as mock_image, \
         patch("webhook_server.handle_document_message") as mock_doc, \
         patch("webhook_server._send_whatsapp_reply"):
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_image.assert_called_once()
    mock_doc.assert_not_called()


def test_a_failed_document_download_gets_a_reply(client):
    raw = json.dumps(_document_payload("972500000000", "application/pdf", "x.pdf")).encode()
    with patch("webhook_server.media_tools.download_media", return_value=(None, None)), \
         patch("webhook_server.handle_document_message") as mock_doc, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post("/webhook", data=raw, content_type="application/json",
                        headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200
    mock_doc.assert_not_called()
    assert "שוב" in mock_send.call_args[0][1]


def test_a_document_its_reader_supports_enters_the_conversation(monkeypatch):
    """WhatsApp's download URL is short-lived: the text must enter the
    conversation now, marked with the filename, or it is gone for good."""
    seen = {}
    monkeypatch.setattr(assistant, "handle_whatsapp_message",
                        lambda msg, sender_id="": seen.update(msg=msg) or "תשובה")
    reply = assistant.handle_document_message("name,city\na,רמלה\n".encode("utf-8"), "סניפים.csv",
                                              "text/csv", "", sender_id="s")
    assert reply == "תשובה"
    assert "סניפים.csv" in seen["msg"]
    assert "רמלה" in seen["msg"]


def test_a_document_its_reader_cannot_handle_gets_the_ready_made_explanation(monkeypatch):
    """attachment_readers' refusal is already user-ready Hebrew - passing it
    through the model would only rephrase an explanation at token cost."""
    called = []
    monkeypatch.setattr(assistant, "handle_whatsapp_message",
                        lambda *a, **k: called.append(a) or "x")
    reply = assistant.handle_document_message(b"PK", "מצגת.pptx", "application/x", "", sender_id="s")
    assert not called
    assert "מצגת" in reply or "❌" in reply


def test_a_long_document_says_it_was_cut(monkeypatch):
    seen = {}
    monkeypatch.setattr(assistant, "handle_whatsapp_message",
                        lambda msg, sender_id="": seen.update(msg=msg) or "ok")
    long_text = "\n".join(f"שורה {i} עם קצת תוכן כדי למלא" for i in range(2000))
    import attachment_readers
    monkeypatch.setattr(attachment_readers, "extract_text",
                        lambda *a, **k: f"[חלק 1 מתוך 4]\n{long_text[:100]}\n\n[סוף חלק 1. יש עוד 3 חלקים.]")
    assistant.handle_document_message(b"x", "גדול.csv", "text/csv", "", sender_id="s")
    assert "נקטע" in seen["msg"]


# --- multi-tab spreadsheets (the Drive export fix, proven end to end) -------


def test_a_two_sheet_workbook_reads_both_tabs():
    """What the xlsx export buys: the second tab is data, not silence."""
    import io
    from openpyxl import Workbook
    import attachment_readers

    wb = Workbook()
    wb.active.title = "ינואר"
    wb.active.append(["סניף", "מכירות"])
    wb.active.append(["רמלה", 10])
    second = wb.create_sheet("פברואר")
    second.append(["סניף", "מכירות"])
    second.append(["לוד", 20])
    buf = io.BytesIO()
    wb.save(buf)

    text = attachment_readers.extract_text("מכירות.xlsx", buf.getvalue())
    assert "ינואר" in text and "פברואר" in text
    assert "רמלה" in text and "לוד" in text


def test_a_voice_note_falls_back_to_whisper_when_gemini_is_out(fake_client, monkeypatch):
    """Gemini's 429 must not make the bot deaf: the transcript comes from the
    fallback tier and the reply then goes through the normal text path."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: False)
    assistant._fallback_sessions.clear()

    quota_error = assistant.genai_errors.ClientError(429, {"error": {"message": "quota"}})
    fake_client.models.generate_content.side_effect = quota_error
    monkeypatch.setattr(assistant.llm, "transcribe", lambda b, m: "תקבע לי פגישה מחר בחמש")

    chat = MagicMock()
    chat.send_message.return_value = MagicMock(text="נרשם")
    fake_client.chats.create.return_value = chat

    reply = assistant.handle_voice_message(b"ogg-bytes", "audio/ogg", sender_id="sender-v")
    assert reply == "נרשם"
    sent = chat.send_message.call_args.args[0]
    assert "תקבע לי פגישה מחר בחמש" in sent
    assistant._fallback_sessions.clear()


def test_a_voice_note_no_tier_can_hear_asks_for_a_resend(fake_client, monkeypatch):
    fake_client.models.generate_content.side_effect = RuntimeError("boom")
    monkeypatch.setattr(assistant.llm, "transcribe", lambda b, m: None)
    reply = assistant.handle_voice_message(b"ogg-bytes", "audio/ogg", sender_id="sender-v")
    assert "שוב" in reply
