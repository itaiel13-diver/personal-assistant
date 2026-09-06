import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

import webhook_server


@pytest.fixture
def client():
    webhook_server.app.testing = True
    return webhook_server.app.test_client()


def _sign(raw_body: bytes) -> str:
    digest = hmac.new(webhook_server.META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _text_message_payload(sender: str, text: str) -> dict:
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
                        "text": {"body": text},
                        "type": "text",
                    }],
                },
                "field": "messages",
            }],
        }],
    }


def _image_message_payload(sender: str) -> dict:
    """A real message, but of a type this bot doesn't handle (voice notes and
    locations look the same shape-wise - just a different 'type' and no 'text' key)."""
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
                        "image": {"id": "media-id", "mime_type": "image/jpeg"},
                        "type": "image",
                    }],
                },
                "field": "messages",
            }],
        }],
    }


def _status_update_payload() -> dict:
    """Meta also posts delivery/read receipts to the same webhook - no 'messages' key."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry-id",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "123"},
                    "statuses": [{"id": "wamid.test", "status": "delivered"}],
                },
                "field": "messages",
            }],
        }],
    }


def test_health_check(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.data == b"OK"


def test_verify_webhook_success(client):
    r = client.get("/webhook", query_string={
        "hub.mode": "subscribe",
        "hub.verify_token": webhook_server.META_VERIFY_TOKEN,
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 200
    assert r.data.decode() == "1158201444"


def test_verify_webhook_wrong_token_rejected(client):
    r = client.get("/webhook", query_string={
        "hub.mode": "subscribe",
        "hub.verify_token": "not-the-real-token",
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 403


def test_verify_webhook_wrong_mode_rejected(client):
    r = client.get("/webhook", query_string={
        "hub.mode": "unsubscribe",
        "hub.verify_token": webhook_server.META_VERIFY_TOKEN,
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 403


def test_post_without_signature_is_rejected(client):
    body = json.dumps(_text_message_payload("972500000000", "hi")).encode()
    r = client.post("/webhook", data=body, content_type="application/json")
    assert r.status_code == 403


def test_post_with_wrong_signature_is_rejected(client):
    body = json.dumps(_text_message_payload("972500000000", "hi")).encode()
    r = client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert r.status_code == 403


def test_valid_text_message_triggers_reply(client):
    body = json.dumps(_text_message_payload("972500000000", "שלום")).encode()
    with patch("webhook_server.handle_whatsapp_message", return_value="תשובת בדיקה") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": _sign(body)},
        )
    assert r.status_code == 200
    mock_handle.assert_called_once_with("שלום", sender_id="972500000000")
    mock_send.assert_called_once_with("972500000000", "תשובת בדיקה")


def test_unsupported_message_type_gets_graceful_reply_not_silence(client):
    """An image/voice-note/location message must not be a black hole -
    Gemini isn't called (nothing to feed it), but the sender gets a reply."""
    body = json.dumps(_image_message_payload("972500000000")).encode()
    with patch("webhook_server.handle_whatsapp_message") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": _sign(body)},
        )
    assert r.status_code == 200
    mock_handle.assert_not_called()
    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "972500000000"


def test_status_update_is_acknowledged_without_processing(client):
    """A delivery/read receipt must not crash the handler or trigger a reply -
    Meta sends these to the same webhook URL as actual messages."""
    body = json.dumps(_status_update_payload()).encode()
    with patch("webhook_server.handle_whatsapp_message") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send:
        r = client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": _sign(body)},
        )
    assert r.status_code == 200
    mock_handle.assert_not_called()
    mock_send.assert_not_called()


def test_send_whatsapp_reply_calls_graph_api_correctly():
    with patch("webhook_server.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        webhook_server._send_whatsapp_reply("972500000000", "שלום")
    args, kwargs = mock_post.call_args
    assert args[0] == f"https://graph.facebook.com/{webhook_server.GRAPH_API_VERSION}/test-phone-number-id/messages"
    assert kwargs["headers"]["Authorization"] == "Bearer test-whatsapp-token"
    assert kwargs["json"]["to"] == "972500000000"
    assert kwargs["json"]["text"]["body"] == "שלום"


def test_send_whatsapp_reply_does_not_raise_on_api_error():
    with patch("webhook_server.requests.post") as mock_post:
        mock_post.return_value.status_code = 401
        mock_post.return_value.text = "invalid token"
        webhook_server._send_whatsapp_reply("972500000000", "שלום")  # must not raise


def test_send_whatsapp_reply_does_not_raise_on_network_error():
    import requests
    with patch("webhook_server.requests.post", side_effect=requests.ConnectionError("boom")):
        webhook_server._send_whatsapp_reply("972500000000", "שלום")  # must not raise


def test_malformed_payload_does_not_crash(client):
    body = json.dumps({"unexpected": "shape"}).encode()
    with patch("webhook_server.handle_whatsapp_message") as mock_handle:
        r = client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": _sign(body)},
        )
    assert r.status_code == 200
    mock_handle.assert_not_called()


def test_a_long_reply_is_split_instead_of_being_rejected():
    """Meta rejects a body over 4096 chars. The rejection was only logged, so a
    long answer reached the sender as complete silence."""
    parts = webhook_server._split_for_whatsapp("א" * 10_000)
    assert len(parts) > 1
    assert all(len(p) <= webhook_server.WHATSAPP_MAX_BODY for p in parts)
    assert "".join(parts) == "א" * 10_000


def test_splitting_prefers_line_boundaries_so_rows_stay_intact():
    rows = "\n".join(f"{i}: סניף {i} | רמלה | איתי" for i in range(400))
    for part in webhook_server._split_for_whatsapp(rows):
        for line in part.split("\n"):
            assert line == "" or line.startswith(tuple("0123456789"))


def test_a_short_reply_is_sent_as_one_message():
    assert webhook_server._split_for_whatsapp("שלום") == ["שלום"]


def test_every_chunk_of_a_long_reply_is_actually_sent():
    with patch.object(webhook_server, "requests") as requests_mock:
        requests_mock.post.return_value.status_code = 200
        with patch.object(webhook_server, "WHATSAPP_TOKEN", "t"), \
             patch.object(webhook_server, "PHONE_NUMBER_ID", "p"):
            webhook_server._send_whatsapp_reply("972500000000", "ב" * 9000)
    assert requests_mock.post.call_count == 3
    bodies = [c.kwargs["json"]["text"]["body"] for c in requests_mock.post.call_args_list]
    assert all(b.startswith("(") for b in bodies), "numbering is missing"
    assert all(len(b) <= webhook_server.WHATSAPP_MAX_BODY for b in bodies)
