from unittest.mock import patch

import pytest
from twilio.request_validator import RequestValidator

import webhook_server


@pytest.fixture
def client():
    webhook_server.app.testing = True
    return webhook_server.app.test_client()


def _sign(form: dict) -> str:
    url = "http://localhost/whatsapp"
    return RequestValidator(webhook_server.TWILIO_AUTH_TOKEN).compute_signature(url, form)


def test_health_check(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.data == b"OK"


def test_unsigned_webhook_request_is_rejected(client):
    """A forged POST without a valid Twilio signature must never reach Gemini."""
    r = client.post("/whatsapp", data={"Body": "hi", "From": "whatsapp:+972500000000"})
    assert r.status_code == 403


def test_wrong_signature_is_rejected(client):
    r = client.post(
        "/whatsapp",
        data={"Body": "hi", "From": "whatsapp:+972500000000"},
        headers={"X-Twilio-Signature": "not-a-real-signature"},
    )
    assert r.status_code == 403


def test_signed_webhook_request_is_accepted_and_replies(client):
    form = {"Body": "שלום", "From": "whatsapp:+972500000009"}
    with patch("webhook_server.handle_whatsapp_message", return_value="תשובת בדיקה") as mock_handle:
        r = client.post("/whatsapp", data=form, headers={"X-Twilio-Signature": _sign(form)})
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "תשובת בדיקה" in body
    mock_handle.assert_called_once_with("שלום", sender_id="whatsapp:+972500000009")


def test_empty_body_gets_default_reply_without_calling_gemini(client):
    form = {"Body": "", "From": "whatsapp:+972500000009"}
    with patch("webhook_server.handle_whatsapp_message") as mock_handle:
        r = client.post("/whatsapp", data=form, headers={"X-Twilio-Signature": _sign(form)})
    assert r.status_code == 200
    assert "לא התקבלה הודעה" in r.data.decode("utf-8")
    mock_handle.assert_not_called()
