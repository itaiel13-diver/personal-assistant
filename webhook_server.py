import hashlib
import hmac
import logging
import os

import requests
from flask import Flask, Response, abort, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

import media_tools
import proactive
import storage
from assistant import (handle_document_message, handle_image_message,
                         handle_voice_message, handle_whatsapp_message)

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
# The shared secret an external pinger presents to /tick. Unset, the
# endpoint does not exist at all - an unguarded /tick is a stranger's
# button for making the assistant message Itai.
TICK_SECRET = os.environ.get("TICK_SECRET", "")

# The one person this assistant serves, in international form without a plus
# (9725...). When set, messages from any other number are dropped before they
# reach the assistant - see _is_owner for why the check lives here and why a
# stranger gets silence rather than a polite refusal. Unset, every sender is
# accepted: that is the fallback the proactive side already relies on (it
# writes to whoever messaged last), and a lock whose key was never cut would
# lock Itai out of his own assistant.
OWNER_PHONE = os.environ.get("OWNER_PHONE", "").strip()


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
    is always a separate, explicit outbound call to the Graph API.

    Returns True when every part was accepted. A reply to an incoming message
    ignores that, but a proactive send needs it: an item whose message never
    left has to be un-claimed so the next tick can try again."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        logger.error("WHATSAPP_TOKEN/PHONE_NUMBER_ID not set — cannot send reply.")
        return False
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
                return False
        except requests.RequestException as e:
            logger.error(f"WhatsApp send raised an exception: {e}")
            return False
    return bool(parts)


def _normalise_number(value: str) -> str:
    """Meta sends phone numbers as digits only; an env var pasted out of a
    contacts app may carry a plus, spaces or dashes. Digits are the common
    ground, so the comparison keeps only them."""
    return "".join(ch for ch in value if ch.isdigit())


def _is_owner(sender: str) -> bool:
    """Whether this sender is the person the assistant exists for.

    The Meta signature check proves the POST came from Meta. It says nothing
    about who pressed send: anyone who messages the bot's business number
    reaches this handler, and behind it sits an assistant that can read Itai's
    mail, files, calendar and tasks. With OWNER_PHONE configured, every other
    number is dropped here, before a single tool exists for them.

    Dropped means silent, not a courteous "this bot is private". A reply
    confirms to a stranger that the number is a live bot wired to someone's
    accounts - exactly the reconnaissance the check is meant to deny - and a
    wrong number already looks like silence, so silence costs nothing.

    The check runs before note_inbound on purpose: the proactive side sends to
    the most recent inbound number, so recording a stranger's message would
    aim the next reminder at the stranger.
    """
    if not OWNER_PHONE:
        return True
    return _normalise_number(sender) == _normalise_number(OWNER_PHONE)


def _extract_incoming_message(payload: dict):
    """Returns (sender, text, message_type, media, message_id) for the first
    message in a Meta webhook payload, or (None, None, None, None, None) for
    non-message events
    (delivery/read receipts, template status updates, etc.) which Meta also
    sends to this same webhook. message_type is Meta's own type string
    ('text', 'image', 'audio', 'location', ...) so the caller can tell a real
    but unsupported message (which deserves a reply) apart from no message
    at all (which doesn't).

    For media messages the payload carries no bytes - only a pointer. media is
    then {'id', 'mime_type', 'caption'} taken from the message's image/audio
    block; caption exists only on images, and is empty for a voice note."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None, None, None, None, None
        message = messages[0]
        sender = message.get("from")
        message_id = message.get("id")
        message_type = message.get("type", "unknown")
        text = message.get("text", {}).get("body", "") if message_type == "text" else ""
        media = None
        if message_type in ("image", "audio", "document"):
            info = message.get(message_type) or {}
            media = {
                "id": info.get("id"),
                "mime_type": info.get("mime_type", ""),
                "caption": info.get("caption", ""),
                # Only documents carry a filename, and for a document it is
                # the dispatch signal - the extension decides how it is read.
                "filename": info.get("filename", ""),
            }
        return sender, text, message_type, media, message_id
    except (KeyError, IndexError, TypeError):
        return None, None, None, None, None


# Meta retries a webhook delivery until it gets a fast 200, and a slow answer
# (quota waits, tool loops) arrives again and again. Every retry must be a
# no-op: Postgres remembers across restarts, the in-memory set covers the
# no-database case within one process.
_SEEN_MESSAGE_IDS = set()
_SEEN_MESSAGE_IDS_LIMIT = 5000


def _message_already_processed(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in _SEEN_MESSAGE_IDS:
        return True
    return storage.message_seen(message_id)


def _mark_message_processed(message_id: str) -> None:
    if not message_id:
        return
    if len(_SEEN_MESSAGE_IDS) >= _SEEN_MESSAGE_IDS_LIMIT:
        _SEEN_MESSAGE_IDS.clear()
    _SEEN_MESSAGE_IDS.add(message_id)
    storage.mark_message(message_id)


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
    sender, text, message_type, media, message_id = _extract_incoming_message(payload)

    if sender and not _is_owner(sender):
        logger.warning(f"Message from a non-owner number ({sender}) - ignored.")
        return "OK", 200

    if message_id:
        if _message_already_processed(message_id):
            logger.info(f"Duplicate delivery of {message_id} - acknowledged, not reprocessed.")
            return "OK", 200
        _mark_message_processed(message_id)

    if sender:
        # Itai writing is what reopens WhatsApp's 24-hour window, and the
        # proactive side has no other way to know when that happened. Recorded
        # for any message type - even a video we cannot watch reopens it too.
        storage.note_inbound(sender)

    if sender and message_type == "text" and text:
        reply_text = handle_whatsapp_message(text.strip(), sender_id=sender)
        _send_whatsapp_reply(sender, reply_text)
    elif sender and message_type == "image" and media and media.get("id"):
        image_bytes, mime = media_tools.download_media(media["id"])
        if image_bytes:
            reply_text = handle_image_message(
                image_bytes, mime or media["mime_type"],
                media.get("caption", ""), sender_id=sender)
        else:
            reply_text = "קיבלתי ששלחת תמונה, אבל ההורדה שלה מוואטסאפ נכשלה. אפשר לשלוח אותה שוב?"
        _send_whatsapp_reply(sender, reply_text)
    elif sender and message_type == "audio" and media and media.get("id"):
        audio_bytes, mime = media_tools.download_media(media["id"])
        if audio_bytes:
            reply_text = handle_voice_message(audio_bytes, mime or media["mime_type"], sender_id=sender)
        else:
            reply_text = "קיבלתי ששלחת הודעה קולית, אבל ההורדה שלה מוואטסאפ נכשלה. אפשר לשלוח אותה שוב?"
        _send_whatsapp_reply(sender, reply_text)
    elif sender and message_type == "document" and media and media.get("id"):
        data, mime = media_tools.download_media(media["id"])
        if not data:
            reply_text = "קיבלתי ששלחת קובץ, אבל ההורדה שלו מוואטסאפ נכשלה. אפשר לשלוח אותו שוב?"
        else:
            base = media_tools.base_mime(mime or media["mime_type"])
            caption = media.get("caption", "")
            # People send photos as documents to keep the original quality, and
            # voice messages occasionally arrive as files - dispatch on what the
            # bytes are, not on which button he pressed.
            if base.startswith("image/"):
                reply_text = handle_image_message(data, base, caption, sender_id=sender)
            elif base.startswith("audio/"):
                reply_text = handle_voice_message(data, base, sender_id=sender)
            else:
                reply_text = handle_document_message(
                    data, media.get("filename", ""), base, caption, sender_id=sender)
        _send_whatsapp_reply(sender, reply_text)
    elif sender and message_type is not None:
        # A real message of a type we don't handle (video, location...) -
        # reply so the person knows the bot saw it, instead of silence that looks broken.
        logger.info(f"Unsupported message type '{message_type}' from {sender} — replying with guidance.")
        _send_whatsapp_reply(sender, "כרגע אני מבין טקסט, תמונות, מסמכים והודעות קוליות. את זה אפשר לתאר במילים? 🙂")
    else:
        logger.info("Webhook event with no incoming message (status update, etc.) — ignored.")

    # Meta requires a fast 2xx regardless of content; a non-2xx (or a slow
    # response) makes it retry, and repeated failures can disable the webhook.
    return "OK", 200


def _tick_is_authorised(req) -> bool:
    """The pinger proves itself with a shared secret, by header or query string.
    The header is preferred - a query string ends up in access logs."""
    provided = req.headers.get("X-Tick-Secret") or req.args.get("key", "")
    return hmac.compare_digest(provided, TICK_SECRET)


@app.route("/tick", methods=["GET", "POST"])
def tick():
    """The assistant's heartbeat, called from outside every few minutes.

    Render's free plan sleeps the instance after about fifteen minutes, so this
    request does two jobs: it wakes the service, and it gives the proactive
    routines their only chance to look at the clock. It is deliberately cheap -
    on the overwhelming majority of calls nothing is due and it returns
    immediately.
    """
    if not TICK_SECRET:
        # Not configured: behave as though the route was never added, rather
        # than advertise an endpoint that anyone may press.
        abort(404)
    if not _tick_is_authorised(request):
        abort(403)

    try:
        summary = proactive.run_tick(send=_send_whatsapp_reply)
    except Exception:
        # A crash here would make the pinger see failures and, on some free
        # services, stop calling - which silently ends every routine.
        logger.exception("Proactive tick failed.")
        return jsonify({"ok": False}), 200

    if summary.get("due"):
        logger.info(f"Proactive tick: {summary}")
    return jsonify(summary), 200


# Google's OAuth consent screen requires a home page, a privacy policy and
# terms of service, each on a domain registered under "Authorized domains".
# The Render service is the only domain we actually control, so the three
# pages are served from here rather than invented somewhere else. They are
# static text: no data is collected by them, and no template engine is used.

_PAGE = """<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Arial,sans-serif;
max-width:44rem;margin:0 auto;padding:2rem 1.25rem;line-height:1.7;
color:#1c1a17;background:#fbfaf8}}
h1{{font-size:1.6rem;margin:0 0 .25rem}}
p.sub{{color:#6b645c;margin:0 0 2rem}}
h2{{font-size:1.05rem;margin:2rem 0 .5rem}}
a{{color:#8a5a2b}}
</style></head><body>
<h1>{title}</h1><p class="sub">{sub}</p>{body}</body></html>"""


@app.route("/", methods=["GET"])
def health_check():
    # Render's health check hits this path; it only needs a 2xx.
    return _PAGE.format(
        title="עוזר אישי",
        sub="Personal Assistant \u2014 a private WhatsApp assistant, single user",
        body="""
<p>שירות פרטי המחבר את WhatsApp לחשבון Google של בעליו היחיד, כדי לקרוא
ולסכם דואר, קבצים, יומן ומשימות עבורו בלבד.</p>
<p>This is a private, single-user service. It connects one person's WhatsApp
to that same person's own Google account so they can read and organise their
own mail, files, calendar and tasks. It is not offered to other users.</p>
<p><a href="/privacy">Privacy policy</a> &middot;
<a href="/terms">Terms of service</a></p>""",
    ), 200


@app.route("/privacy", methods=["GET"])
def privacy_policy():
    return _PAGE.format(
        title="מדיניות פרטיות / Privacy policy",
        sub="Last updated 2026-09-07",
        body="""
<h2>מי מפעיל את השירות</h2>
<p>שירות פרטי בהפעלת אדם יחיד, עבור חשבון Google אחד \u2014 שלו. אין משתמשים
אחרים ואין הרשמה.</p>
<h2>איזה מידע נאסף</h2>
<p>הודעות WhatsApp שהבעלים שולח לשירות, ומידע מחשבון Google שלו (דואר,
קבצים ב-Drive, יומן, אנשי קשר ומשימות) \u2014 רק כשהוא מבקש זאת מפורשות
בהודעה.</p>
<h2>מה נעשה במידע</h2>
<p>המידע משמש אך ורק כדי לענות לבעלים באותה שיחה. הוא אינו נמכר, אינו מושכר
ואינו משותף עם צד שלישי, למעט ספקי התשתית שהשירות רץ עליהם (Render,
Anthropic, Meta WhatsApp Business API) לצורך אספקת התשובה עצמה.</p>
<h2>שמירה ומחיקה</h2>
<p>היסטוריית השיחה נשמרת בבסיס נתונים פרטי של הבעלים. הבעלים יכול למחוק
אותה בכל רגע, ולבטל את גישת השירות לחשבון Google שלו בכל רגע בכתובת
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>
<h2>Google user data</h2>
<p>The service's use of information received from Google APIs adheres to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google
API Services User Data Policy</a>, including the Limited Use requirements.
Data from Google APIs is used only to answer the owner's own requests, is
never transferred to anyone except as needed to provide that answer, is never
used for advertising, and is never read by a human other than the owner.</p>
<h2>יצירת קשר / Contact</h2>
<p>itaiel13@gmail.com</p>""",
    ), 200


@app.route("/terms", methods=["GET"])
def terms_of_service():
    return _PAGE.format(
        title="תנאי שימוש / Terms of service",
        sub="Last updated 2026-09-07",
        body="""
<h2>למי השירות מיועד</h2>
<p>השירות פרטי ומיועד לבעליו בלבד. אין הרשאה לאדם אחר להשתמש בו.</p>
<h2>אין אחריות</h2>
<p>השירות ניתן כפי שהוא (AS IS), ללא אחריות מכל סוג. תשובות נוצרות על ידי
מודל שפה ועלולות להיות שגויות; אין להסתמך עליהן לצורך החלטה משפטית, רפואית
או פיננסית.</p>
<h2>הגבלת אחריות</h2>
<p>המפעיל אינו נושא באחריות לכל נזק הנובע משימוש בשירות.</p>
<h2>שינוי והפסקה</h2>
<p>המפעיל רשאי לשנות או להפסיק את השירות בכל עת וללא הודעה מוקדמת.</p>
<h2>יצירת קשר / Contact</h2>
<p>itaiel13@gmail.com</p>""",
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
