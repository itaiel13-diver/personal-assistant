import hashlib
import hmac
import logging
import os

import requests
from flask import Flask, Response, abort, request
from werkzeug.middleware.proxy_fix import ProxyFix

from assistant import handle_whatsapp_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Render (and most PaaS) terminate TLS at a proxy and forward requests as
# plain HTTP internally. Without this, request.url is http://... which
# breaks anything relying on the public scheme.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN")
META_APP_SECRET = os.environ.get("META_APP_SECRET")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")


def _is_valid_meta_signature(req) -> bool:
    """Confirms a webhook POST actually came from Meta, not a spoofed request.
    Meta signs the raw request body with the app secret (HMAC-SHA256)."""
    if not META_APP_SECRET:
        logger.error("META_APP_SECRET is not set — rejecting webhook request.")
        return False
    signature_header = req.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(META_APP_SECRET.encode("utf-8"), req.get_data(), hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


# Meta rejects a text body over 4096 characters outright. The rejection was only
# logged, so a long answer reached the sender as nothing at all - which looks
# identical to the assistant ignoring the question.
WHATSAPP_MAX_BODY = 4096


def _split_for_whatsapp(text: str, limit: int = WHATSAPP_MAX_BODY) -> list:
    """Splits a long reply into sendable chunks, preferring paragraph then line
    boundaries so a table of results is not cut through the middle of a row."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for block in text.split("\n"):
        while len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            # A single line longer than the limit has no boundary to use.
            chunks.append(block[:limit])
            block = block[limit:]
        if not current:
            current = block
        elif len(current) + 1 + len(block) <= limit:
            current = f"{current}\n{block}"
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _send_whatsapp_reply(to: str, text: str) -> None:
    """Sends a message back via the WhatsApp Cloud API.
    Unlike Twilio, Meta has no synchronous webhook-response reply - a reply
    is always a separate, explicit outbound call to the Graph API."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        logger.error("WHATSAPP_TOKEN/PHONE_NUMBER_ID not set — cannot send reply.")
        return
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    parts = _split_for_whatsapp(text)
    for index, part in enumerate(parts, start=1):
        if len(parts) > 1:
            part = f"({index}/{len(parts)})\n{part}"[:WHATSAPP_MAX_BODY]
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": part},
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            if r.status_code >= 400:
                logger.error(f"WhatsApp send failed: {r.status_code} {r.text}")
                return
        except requests.RequestException as e:
            logger.error(f"WhatsApp send raised an exception: {e}")
            return


def _extract_incoming_message(payload: dict):
    """Returns (sender, text, message_type) for the first message in a Meta
    webhook payload, or (None, None, None) for non-message events
    (delivery/read receipts, template status updates, etc.) which Meta also
    sends to this same webhook. message_type is Meta's own type string
    ('text', 'image', 'audio', 'location', ...) so the caller can tell a real
    but unsupported message (which deserves a reply) apart from no message
    at all (which doesn't)."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None, None, None
        message = messages[0]
        sender = message.get("from")
        message_type = message.get("type", "unknown")
        text = message.get("text", {}).get("body", "") if message_type == "text" else ""
        return sender, text, message_type
    except (KeyError, IndexError, TypeError):
        return None, None, None


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta calls this once, synchronously, when you save the webhook URL in
    the App dashboard, to prove you control this endpoint."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and META_VERIFY_TOKEN and token == META_VERIFY_TOKEN:
        return Response(challenge, status=200, mimetype="text/plain")
    abort(403)


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    if not _is_valid_meta_signature(request):
        abort(403)

    payload = request.get_json(silent=True) or {}
    sender, text, message_type = _extract_incoming_message(payload)

    if sender and message_type == "text" and text:
        reply_text = handle_whatsapp_message(text.strip(), sender_id=sender)
        _send_whatsapp_reply(sender, reply_text)
    elif sender and message_type is not None:
        # A real message of a type we don't handle (voice note, image, location...) -
        # reply so the person knows the bot saw it, instead of silence that looks broken.
        logger.info(f"Unsupported message type '{message_type}' from {sender} — replying with guidance.")
        _send_whatsapp_reply(sender, "כרגע אני תומך רק בהודעות טקסט. אפשר לתאר את זה במילים? 🙂")
    else:
        logger.info("Webhook event with no incoming message (status update, etc.) — ignored.")

    # Meta requires a fast 2xx regardless of content; a non-2xx (or a slow
    # response) makes it retry, and repeated failures can disable the webhook.
    return "OK", 200


@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
