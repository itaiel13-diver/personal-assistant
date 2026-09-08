"""Which of three drawers an incoming email belongs in.

Until now the mail watch sent Itai one WhatsApp message per unread email in the
primary inbox. That is honest and it is also a firehose: a Samsung newsletter,
a Connecteam system notice and an actual question from a store manager all
arrive as the same buzz in his pocket while he is driving between branches.

The drawers, borrowed from LangChain's executive-ai-assistant, which sorts
every message before it reaches the person:

    ignore  - machine mail. He is never told. It is marked as seen and dropped.
    notify  - a human wrote something he should know about. One message, as
              before.
    reply   - someone is waiting on an answer from him. Same message, flagged,
              because this is the drawer that costs money when it is missed.

Rules decide first, and the model only sees what the rules could not settle.
That order is not an optimisation, it is the safety property: a rule is
inspectable and stable, so the mail that is silently dropped is dropped for a
reason written down here, not for a reason a model had that day.

Only structural evidence may silence a message. A List-Unsubscribe header, a
Gmail bulk category, a no-reply address, the footer a mailing platform stamps
on the bottom of what it sends - these are facts about how the mail was sent,
and a colleague typing a sentence cannot produce any of them. What a message is
*about* never sends it to the ignore drawer, because the cost of the two
mistakes is not symmetric: a newsletter that gets through is a buzz he ignores,
and a question that gets dropped is a customer waiting for three days. That is
why the list below holds no topic words - only boilerplate.

The model calls skip Gemini deliberately (see llm.py). The Gemini free tier is
twenty requests a day and those belong to questions Itai actually asked; Groq's
free tier is a thousand a day and can afford to read his mail. With no Groq key
set, llm returns nothing, every unclear message defaults to notify, and the
behaviour is exactly what it was before this module existed.
"""
import logging
import re

import llm

logger = logging.getLogger(__name__)

IGNORE = "ignore"
NOTIFY = "notify"
REPLY = "reply"
DRAWERS = (IGNORE, NOTIFY, REPLY)

# Gmail's own bulk classifications. The inbox query already asks for
# category:primary, so these are a second belt - a redeploy or a hand-edited
# query should not be able to turn the newsletter tap back on.
BULK_LABELS = frozenset({
    "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS", "SPAM", "TRASH",
})

# Addresses nobody reads a reply from. Matched against the whole From header,
# so a display name of "Samsung Newsletter" counts too.
_MACHINE_SENDER = re.compile(
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon"
    r"|postmaster@|bounces?@|newsletter|mailchimp|sendgrid|mailer@|automated?@",
    re.IGNORECASE,
)

# The footer a mailing platform adds to what it sends. Every entry is
# boilerplate - a sentence about the mailing itself, not about anything. A
# topic word would belong nowhere near this list: "מבצע", "הנחה", "עדכון",
# "ניוזלטר" and "webinar" are all words Itai's actual job is made of, and a
# colleague can write any of them in a message that matters.
_BULK_PHRASES = (
    "unsubscribe", "view this email in your browser", "you are receiving this",
    "manage your preferences", "email preferences",
    "להסרה מרשימת התפוצה", "להסרה מהרשימה", "הסרה מרשימת התפוצה",
    "לצפייה במייל בדפדפן", "אם אינך מעוניין לקבל", "הוסר מרשימת התפוצה",
)

# Someone is waiting on him. A question mark is not enough on its own - half
# the subject lines in a marketing inbox end in one - but an explicit ask is.
_ANSWER_WANTED = (
    "מחכה לתשובה", "ממתין לתשובה", "אשמח לתשובה", "אשמח לעדכון", "נא לאשר",
    "לאישורך", "מבקש אישור", "מאשר?", "תוכל לאשר", "תוכל לעדכן", "תעדכן אותי",
    "אשמח אם", "מתי תוכל", "עד מתי", "תחזור אליי", "חוזר אליי", "דחוף",
    "בהקדם", "נא להשיב", "מה קורה עם", "האם תוכל",
    "please confirm", "please advise", "please reply", "let me know",
    "waiting for your", "awaiting your", "can you", "could you", "asap",
    "action required", "response needed", "by end of day",
)

# How many unsettled messages are described to the model in one request. One
# request per tick, not one per message: the batch is cheaper, and the model
# sorting them side by side is more consistent than the same model asked eight
# separate times.
MODEL_BATCH = 8

_SYSTEM = (
    "You sort the inbox of Itai, a field region manager for Samsung displays in "
    "Israeli stores. He is on the road most of the day and reads these as WhatsApp "
    "buzzes. Sort each email into exactly one drawer:\n"
    "ignore - automated, bulk, marketing, or a system notice he does not act on.\n"
    "notify - a real person or a real work matter he should know about, but nobody "
    "is waiting on him.\n"
    "reply - somebody is waiting for an answer or a decision from him.\n"
    "When you are unsure between ignore and notify, choose notify: a missed work "
    "email costs him more than one he glances at. Answer with JSON only."
)


def _text_of(message: dict) -> str:
    return " ".join((
        str(message.get("sender") or ""),
        str(message.get("subject") or ""),
        str(message.get("snippet") or ""),
    ))


def is_bulk(message: dict) -> bool:
    """Structural evidence that this was sent to a list, not to Itai."""
    # Present on every mail sent through a mailing platform, and on nothing a
    # person types. The single strongest signal available, and it is free.
    if (message.get("list_unsubscribe") or "").strip():
        return True
    labels = message.get("labels") or []
    if any(label in BULK_LABELS for label in labels):
        return True
    if _MACHINE_SENDER.search(str(message.get("sender") or "")):
        return True
    haystack = _text_of(message).lower()
    return any(phrase in haystack for phrase in _BULK_PHRASES)


def wants_an_answer(message: dict) -> bool:
    haystack = _text_of(message).lower()
    return any(phrase in haystack for phrase in _ANSWER_WANTED)


def by_rules(message: dict):
    """The drawer the rules are sure about, or None to hand it to the model."""
    if is_bulk(message):
        return IGNORE
    if wants_an_answer(message):
        return REPLY
    return None


def _describe(message: dict) -> str:
    subject = (message.get("subject") or "(no subject)").strip()
    snippet = (message.get("snippet") or "").strip()[:240]
    return f"From: {message.get('sender', '')}\nSubject: {subject}\nPreview: {snippet}"


def _ask_model(messages: list) -> dict:
    """Returns {index: drawer} for whatever the model managed to sort.

    Indexes rather than Gmail ids: the ids are long opaque hex strings that a
    model will happily truncate or invent, and a wrong id would misfile a
    different email. A number it cannot get subtly wrong.
    """
    listing = "\n\n".join(
        f"[{i + 1}]\n{_describe(m)}" for i, m in enumerate(messages)
    )
    prompt = (
        f"Sort these {len(messages)} emails.\n\n{listing}\n\n"
        'Answer with one JSON object mapping each number to its drawer, e.g. '
        '{"1": "notify", "2": "ignore"}. Every number must appear exactly once. '
        "No explanation."
    )
    answer = llm.ask_json(prompt, system=_SYSTEM, max_tokens=300, skip=("gemini",))
    if not isinstance(answer, dict):
        return {}

    verdicts = {}
    for key, value in answer.items():
        try:
            index = int(str(key).strip()) - 1
        except ValueError:
            continue
        drawer = str(value).strip().lower()
        if 0 <= index < len(messages) and drawer in DRAWERS:
            verdicts[index] = drawer
    return verdicts


def triage(messages: list, use_model: bool = True) -> dict:
    """Sorts messages into drawers. Returns {message id: drawer}.

    Never raises and never leaves a message unsorted: anything the rules did
    not settle and the model did not answer for comes back as notify, which is
    what the assistant did with every email before there were drawers at all.
    """
    verdicts = {}
    unclear = []
    for message in messages:
        drawer = by_rules(message)
        if drawer:
            verdicts[message["id"]] = drawer
        else:
            unclear.append(message)

    if unclear and use_model:
        batch = unclear[:MODEL_BATCH]
        try:
            for index, drawer in _ask_model(batch).items():
                verdicts[batch[index]["id"]] = drawer
        except Exception as e:
            # A triage that cannot think still has to deliver the mail.
            logger.error(f"Mail triage model step failed: {e}")

    for message in unclear:
        verdicts.setdefault(message["id"], NOTIFY)
    return verdicts
