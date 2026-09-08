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
    # Render's health check only needs a 2xx here. The body is now the home
    # page Google's OAuth consent screen points at, so it is HTML, not "OK".
    r = client.get("/")
    assert r.status_code == 200


def test_the_consent_screen_pages_are_served(client):
    # Google requires the home page, the privacy policy and the terms to
    # resolve on a domain we control. If one of these 404s, the OAuth consent
    # screen cannot be published and the refresh token goes back to expiring
    # every seven days.
    for path in ("/", "/privacy", "/terms"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert b"<!doctype html>" in r.data, path

    body = client.get("/privacy").data.decode("utf-8")
    # The Limited Use wording is what Google checks the policy for.
    assert "Limited Use" in body
    assert "itaiel13@gmail.com" in body


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


def _video_message_payload(sender: str) -> dict:
    """A video: a real message type the bot still does not handle."""
    payload = _image_message_payload(sender)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["video"] = message.pop("image")
    message["type"] = "video"
    return payload


def test_unsupported_message_type_gets_graceful_reply_not_silence(client):
    """A video/document/location message must not be a black hole -
    nothing is fed to the model, but the sender gets a reply."""
    body = json.dumps(_video_message_payload("972500000000")).encode()
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


# --- the heartbeat -------------------------------------------------------
#
# /tick is the only route that makes the assistant speak without being spoken
# to, so who may press it matters more than what it does.


def test_the_tick_endpoint_does_not_exist_until_a_secret_is_configured(client):
    with patch.object(webhook_server, "TICK_SECRET", ""):
        assert client.get("/tick").status_code == 404


def test_the_tick_endpoint_refuses_a_wrong_secret(client):
    with patch.object(webhook_server, "TICK_SECRET", "the-real-secret"):
        assert client.get("/tick?key=guess").status_code == 403
        assert client.get("/tick").status_code == 403
        assert client.get("/tick", headers={"X-Tick-Secret": "guess"}).status_code == 403


def test_the_tick_endpoint_runs_a_pass_for_the_right_secret(client):
    with patch.object(webhook_server, "TICK_SECRET", "the-real-secret"), \
            patch.object(webhook_server.proactive, "run_tick") as run_tick:
        run_tick.return_value = {"due": 0, "sent": []}

        by_header = client.get("/tick", headers={"X-Tick-Secret": "the-real-secret"})
        by_query = client.get("/tick?key=the-real-secret")

    assert by_header.status_code == 200
    assert by_query.status_code == 200
    assert run_tick.call_count == 2


def test_a_failing_tick_still_answers_the_pinger(client):
    # A free pinger that sees repeated failures may stop calling, and a pinger
    # that has stopped ends every proactive routine silently. The tick reports
    # its own failure with a 200 rather than risk that.
    with patch.object(webhook_server, "TICK_SECRET", "the-real-secret"), \
            patch.object(webhook_server.proactive, "run_tick",
                         side_effect=RuntimeError("boom")):
        r = client.get("/tick?key=the-real-secret")
    assert r.status_code == 200
    assert r.get_json() == {"ok": False}


def test_an_incoming_message_reopens_the_free_window(client):
    # The 24-hour window is measured from Itai's last message and nothing else
    # records it. Miss this and every proactive reminder is held forever.
    payload = _text_message_payload("972500000000", "מה יש לי היום")
    raw = json.dumps(payload).encode("utf-8")
    with patch.object(webhook_server, "handle_whatsapp_message", return_value="בסדר"), \
            patch.object(webhook_server, "_send_whatsapp_reply"), \
            patch.object(webhook_server.storage, "note_inbound") as note:
        client.post("/webhook", data=raw,
                    headers={"X-Hub-Signature-256": _sign(raw),
                             "Content-Type": "application/json"})
    note.assert_called_once_with("972500000000")


# --- the owner allowlist ----------------------------------------------------
#
# The Meta signature proves a POST came from Meta, not who pressed send.
# With OWNER_PHONE set, any other number must be dropped before it reaches
# the assistant - and before note_inbound, or the next proactive message
# would be aimed at the stranger.


def _post_text(client, sender):
    body = json.dumps(_text_message_payload(sender, "שלום")).encode()
    return client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": _sign(body)},
    ), body


def test_a_stranger_is_dropped_silently_when_owner_phone_is_set(client, monkeypatch):
    monkeypatch.setattr(webhook_server, "OWNER_PHONE", "972548304072")
    with patch("webhook_server.handle_whatsapp_message") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply") as mock_send, \
         patch("webhook_server.storage.note_inbound") as mock_note:
        r, _ = _post_text(client, "972500000001")
    # Meta still gets its fast 2xx - the webhook must not look broken.
    assert r.status_code == 200
    mock_handle.assert_not_called()
    mock_send.assert_not_called()
    # The proactive side sends to the most recent inbound number, so a
    # stranger's message must never be recorded as the last inbound.
    mock_note.assert_not_called()


def test_the_owner_gets_through(client, monkeypatch):
    monkeypatch.setattr(webhook_server, "OWNER_PHONE", "972548304072")
    with patch("webhook_server.handle_whatsapp_message", return_value="תשובה") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply"):
        r, _ = _post_text(client, "972548304072")
    assert r.status_code == 200
    mock_handle.assert_called_once()


def test_the_number_is_compared_on_digits_only(client, monkeypatch):
    # An env var pasted out of a contacts app may carry a plus and spaces.
    monkeypatch.setattr(webhook_server, "OWNER_PHONE", "+972 54-830-4072")
    with patch("webhook_server.handle_whatsapp_message", return_value="תשובה") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply"):
        r, _ = _post_text(client, "972548304072")
    assert r.status_code == 200
    mock_handle.assert_called_once()


def test_without_owner_phone_the_gate_is_open(client, monkeypatch):
    # Unset means nobody told us which number is Itai's - locking then would
    # lock him out of his own assistant. This test pins that deliberate choice.
    monkeypatch.setattr(webhook_server, "OWNER_PHONE", "")
    with patch("webhook_server.handle_whatsapp_message", return_value="תשובה") as mock_handle, \
         patch("webhook_server._send_whatsapp_reply"):
        r, _ = _post_text(client, "972500000001")
    assert r.status_code == 200
    mock_handle.assert_called_once()
