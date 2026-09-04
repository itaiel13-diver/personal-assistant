import os
import logging

from flask import Flask, request, abort
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

from assistant import handle_whatsapp_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Render (and most PaaS) terminate TLS at a proxy and forward requests as
# plain HTTP internally. Without this, request.url is http://... while
# Twilio signs against the public https://... URL, so signature
# validation below would fail on every single request.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")


def _is_valid_twilio_request(req) -> bool:
    """Confirms a webhook POST actually came from Twilio, not a spoofed request."""
    if not TWILIO_AUTH_TOKEN:
        logger.error("TWILIO_AUTH_TOKEN is not set — rejecting webhook request.")
        return False
    signature = req.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(req.url, req.form, signature)


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    if not _is_valid_twilio_request(request):
        abort(403)

    incoming_text = request.form.get("Body", "").strip()
    sender = request.form.get("From", "unknown")
    logger.info("Incoming WhatsApp message from %s", sender)

    reply_text = (
        handle_whatsapp_message(incoming_text, sender_id=sender)
        if incoming_text
        else "לא התקבלה הודעה."
    )

    twiml = MessagingResponse()
    twiml.message(reply_text)
    return str(twiml), 200, {"Content-Type": "text/xml"}


@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
