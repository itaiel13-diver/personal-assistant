"""The active half of the assistant: what it says when Itai has said nothing.

Everything else in this project answers a message. This module decides, on its
own clock, that something is worth interrupting him about, and says it.

Three facts shape the whole design, and none of them are negotiable:

1. There is no scheduler in the process. The service runs on Render's free
   plan, which stops the instance after about fifteen minutes of quiet, so a
   thread sleeping until 08:55 is a thread that is not running at 08:55. The
   clock lives outside: something free pings /tick every few minutes, the ping
   wakes the instance, and the instance asks this module "is anything due?".
   That makes ticks irregular and occasionally late, so a routine matches a
   window of time rather than an instant - see GRACE.

2. A tick can run twice for the same window. Overlapping pings, a retry, a
   redeploy mid-tick. Nothing here may rely on being called once, so every
   message is claimed in the database before it is sent and the claim is what
   makes it unique - storage.claim().

3. WhatsApp only carries a free-form business message within 24 hours of Itai's
   own last message. Outside that window the only thing Meta will deliver is a
   paid template, and paying is ruled out, so a reminder that falls outside the
   window is reported rather than delivered. In practice one message a day from
   him keeps the window open indefinitely.

There is deliberately no cap on how many messages a tick may send. Itai asked
for an assistant that interrupts as much as it wants; the discipline is that it
never says the same thing twice, not that it says little.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import storage
import triage

logger = logging.getLogger(__name__)

# Real Israel local time, DST included. A fixed +02:00/+03:00 offset would put
# every reminder an hour wrong for half the year.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

FREE_WINDOW = timedelta(hours=24)

# Israel's work week is Sunday to Thursday. Python numbers Monday 0, so Sunday
# is 6 and the set below is Sun, Mon, Tue, Wed, Thu.
WORK_DAYS = frozenset({6, 0, 1, 2, 3})

# How long after its appointed minute a routine still fires. The pinger is a
# free service with no minute-level guarantee and the instance may need half a
# minute to wake, so an exact match would silently drop reminders. Late is fine
# for these: a reminder to clock in is still useful twenty minutes after nine,
# and the message says how late it is.
GRACE = timedelta(minutes=75)

# Set to Itai's number in international form without a plus (e.g. 9725...).
# Left unset, the recipient is whoever wrote to the assistant most recently,
# which on a single-user service is the same person.
OWNER_PHONE = os.environ.get("OWNER_PHONE", "").strip()


@dataclass(frozen=True)
class Due:
    """One thing worth saying, with the identity that stops it being said twice.

    fingerprint must be stable for the same real-world item and different for a
    different one - a date for a daily reminder, a message id for an email.

    preclaimed is for a routine that had to claim before it could decide - the
    mail watch claims a message id so it does not look at the same email on
    every tick, and by then the claim is already made. run_tick then skips the
    claim but still gives it back if the message does not go out.

    on_sent and on_failed are for a routine whose bookkeeping lives somewhere
    other than the proactive log. A reminder is claimed by flipping a row's
    status, so putting it back is an UPDATE on that row rather than a delete
    here, and a repeating one has to be moved to its next occurrence the moment
    this one is delivered. Both are optional and take no arguments.
    """
    fingerprint: str
    kind: str
    text: str
    preclaimed: bool = False
    on_sent: object = None
    on_failed: object = None


def _slot(now: datetime, at: time) -> datetime:
    return now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


def _is_due(now: datetime, at: time) -> int:
    """Returns how many minutes late the tick is for this slot, or -1 if the
    slot is not open: either it has not arrived yet or the grace has passed."""
    behind = now - _slot(now, at)
    if timedelta(0) <= behind < GRACE:
        return int(behind.total_seconds() // 60)
    return -1


# --- attendance ---------------------------------------------------------
#
# The highest-priority routine in the whole assistant, by Itai's own ranking.
# Forgetting to clock in or out of Connecteam costs him real money and is
# entirely preventable by a message arriving at the right minute.

CONNECTEAM_URL = "https://app.connecteam.com"

ATTENDANCE_SLOTS = (
    ("in", time(8, 55), "כניסה"),
    ("out", time(17, 55), "יציאה"),
)


def attendance(now: datetime) -> list:
    """Shift sign-in and sign-out reminders, Sunday to Thursday."""
    if now.weekday() not in WORK_DAYS:
        return []

    due = []
    for slug, at, label in ATTENDANCE_SLOTS:
        late = _is_due(now, at)
        if late < 0:
            continue
        when = _slot(now, at).strftime("%H:%M")
        if late <= 10:
            head = f"⏰ *רישום {label} — {when}*"
        else:
            head = f"⏰ *רישום {label}* — היה אמור להיות ב-{when}, עברו {late} דקות"
        due.append(Due(
            fingerprint=f"attendance:{slug}:{now:%Y-%m-%d}",
            kind="attendance",
            text=f"{head}\nרושם עכשיו ב-Connecteam? {CONNECTEAM_URL}",
        ))
    return due


# --- new mail ------------------------------------------------------------
#
# The half of "active" that Itai described as reading his mail by itself. It
# does so without a single model call, and that is a hard constraint rather
# than a preference: the Gemini free tier allows twenty generate_content
# requests a day - production has already hit that ceiling and answered him
# with an error - so every one of them belongs to a question he actually
# asked. A routine that spent one judging an email would take an answer away
# from him to do it.
#
# So the assistant notices for free and asks. He decides whether it is worth
# reading, and only then does a model call happen, on his instruction, the way
# every other model call in this project does.

# category:primary is Gmail's own classification, which already keeps
# promotions, social and bulk updates out - a filter that would otherwise have
# to be invented, badly, here.
MAIL_QUERY = "is:unread in:inbox category:primary newer_than:1d"

# A cap per tick, not per day. It only exists so that the first tick after a
# quiet weekend does not arrive as twenty messages at once; the rest follow on
# the next tick a few minutes later.
MAIL_PER_TICK = 5

# Mail arriving at 03:00 is read at 07:00 either way. Reminders are pinned to
# the working day, and this keeps the mail watch there too.
WAKING_START = time(7, 0)
WAKING_END = time(22, 30)


def _mail_text(message: dict, drawer: str) -> str:
    subject = message.get("subject") or "(ללא נושא)"
    if drawer == triage.REPLY:
        head = "📬 *מייל שמחכה לתשובה ממך*"
    else:
        head = "📬 *מייל חדש*"
    lines = [head, f"מאת: {message.get('sender', '')}", f"נושא: {subject}"]
    snippet = (message.get("snippet") or "")[:280]
    if snippet:
        lines += ["", snippet]
    # The id is printed in the same [id:...] form search_emails uses, so when
    # Itai answers, the model finds it in its own history and can go straight
    # to read_email instead of searching the mailbox again.
    if drawer == triage.REPLY:
        tail = f'רוצה שאקרא ואציע לך תשובה? תגיד לי [id:{message.get("id")}]'
    else:
        tail = f'רוצה שאקרא ואסכם? תגיד לי [id:{message.get("id")}]'
    lines += ["", tail]
    return "\n".join(lines)


def new_mail(now: datetime) -> list:
    """Raises unread mail Itai has not been told about yet, one message each.

    Not every unread email becomes a message any more. Each one is sorted into
    a drawer first (see triage.py) and the ignore drawer is delivered by not
    delivering it: the claim is still written, under a kind of its own, so the
    email is never looked at again and there is a record of what was silenced
    and when. Only notify and reply reach his phone.
    """
    if not (WAKING_START <= now.time() <= WAKING_END):
        return []

    # Imported here rather than at module scope: this pulls in the Google API
    # client, and the heartbeat framework should stay importable without it.
    from gmail_tools import list_inbox_messages

    # Claimed before triage, not after, so an email cannot be sorted twice by
    # two overlapping ticks - and so the ones already raised do not go back
    # through the model on every tick for the rest of the day.
    fresh = []
    for message in list_inbox_messages(MAIL_QUERY, max_results=10):
        if len(fresh) >= MAIL_PER_TICK:
            break
        if storage.claim(f"mail:{message['id']}", "mail"):
            fresh.append(message)
    if not fresh:
        return []

    drawers = triage.triage(fresh)

    due = []
    for message in fresh:
        fingerprint = f"mail:{message['id']}"
        drawer = drawers.get(message["id"], triage.NOTIFY)
        if drawer == triage.IGNORE:
            # Re-stamped rather than released: released would mean "look at it
            # again next tick", and the whole point is that this one is done.
            storage.restamp(fingerprint, "mail-ignored")
            logger.info("Mail %s muted by triage: %s", message["id"], message.get("subject"))
            continue
        due.append(Due(fingerprint, f"mail-{drawer}", _mail_text(message, drawer), preclaimed=True))
    return due


# --- reminders ----------------------------------------------------------
#
# The one routine Itai drives directly: everything here was asked for out loud,
# in a sentence like "תזכיר לי מחר בבוקר לשלוח את הדוח". Which makes it the
# routine with the least room to be wrong - he is expecting this message, at
# roughly this time, and both a miss and a duplicate are immediately obvious.
#
# There are no quiet hours. A reminder set for 06:00 means 06:00; the mail
# watch stays quiet at night because nobody asked for that mail, and nothing
# about that reasoning applies to something he scheduled himself.

REMINDERS_PER_TICK = 5


def _reminder_text(row: dict, now: datetime) -> str:
    late = int((now - row["due_at"].astimezone(ISRAEL_TZ)).total_seconds() // 60)
    stamp = row["due_at"].astimezone(ISRAEL_TZ).strftime("%H:%M")
    # Ticks are irregular, so a reminder can arrive well after its minute.
    # Saying which minute it was for is the difference between a late reminder
    # and a confusing one.
    when = f" (נקבעה ל-{stamp})" if late >= 10 else ""
    return f"⏰ *תזכורת*{when}\n{row['text']}"


def reminders(now: datetime) -> list:
    """Everything Itai scheduled that has now come due."""
    import reminders as reminder_rules

    due = []
    for row in storage.claim_due_reminders(now, limit=REMINDERS_PER_TICK):
        # The row was claimed by the same statement that selected it, so this
        # tick owns it outright - preclaimed, with its own undo.
        nxt = reminder_rules.next_occurrence(
            row["due_at"].astimezone(ISRAEL_TZ), row["recurrence"], now
        )
        reminder_id = row["id"]
        due.append(Due(
            f"reminder:{reminder_id}:{row['due_at'].isoformat()}",
            "reminder",
            _reminder_text(row, now),
            preclaimed=True,
            on_sent=(lambda i=reminder_id, n=nxt: storage.reschedule_reminder(i, n)) if nxt else None,
            on_failed=lambda i=reminder_id: storage.unclaim_reminder(i),
        ))
    return due


# --- mail he sent that nobody answered -----------------------------------
#
# The other direction of the inbox, and the one nobody watches. A store manager
# who never answered about a broken display is not in any inbox, any list or
# any calendar - the request simply stopped existing the moment he sent it.
# Threads he wrote in are cheap to list and cheap to check: if the newest
# message in the thread is still his, nobody wrote back.
#
# This runs once a day rather than every tick. It costs a Gmail call per thread
# and, unlike an unread email, nothing about it changes between 09:30 and 10:00.

CHASE_AT = time(9, 30)

# Three days is long enough that silence means something and short enough that
# the thing he asked for can still arrive on time.
CHASE_QUIET_DAYS = 3

# Past two weeks a nudge is not what fixes it, and raising it only tells him
# something he has already decided to live with.
CHASE_WINDOW_DAYS = 14

# Three a morning. This is the one routine whose backlog is unbounded on the
# first run - every unanswered mail of the last fortnight arrives at once - and
# thirty messages at 09:30 is how he learns to ignore the assistant.
CHASE_PER_DAY = 3

# Python numbers Monday 0.
HEBREW_DAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")


def _person(header: str) -> str:
    """'Dana Levi <dana@impact.co.il>' -> 'Dana Levi'. The name is what he
    recognises; the address is noise on a phone screen."""
    value = (header or "").split(",")[0].strip()
    if "<" in value:
        name = value.split("<")[0].strip().strip('"')
        if name:
            return name
        value = value.split("<")[1].rstrip(">").strip()
    return value or "מישהו"


def _chase_text(row: dict, now: datetime) -> str:
    sent = datetime.fromtimestamp(row["sent_at_ms"] / 1000, ISRAEL_TZ)
    days = (now.date() - sent.date()).days
    # Inside a week the day name is what he remembers the mail by; past that it
    # stops being a landmark and the date says more.
    when = f"ביום {HEBREW_DAYS[sent.weekday()]}" if days <= 6 else f"ב-{sent:%d/%m}"
    subject = row.get("subject") or "(ללא נושא)"
    return "\n".join([
        "📮 *מייל שלא נענה*",
        f"שלחת ל{_person(row.get('recipient'))} {when} (לפני {days} ימים):",
        f"נושא: {subject}",
        "",
        f'מאז אין תשובה. רוצה שאזכיר לך לחזור אליהם? תגיד לי מתי — או [id:{row.get("id")}] כדי שאקרא מה שלחת.',
    ])


def unanswered_mail(now: datetime) -> list:
    """Threads Itai wrote in and nobody wrote back to."""
    if now.weekday() not in WORK_DAYS:
        return []
    if _is_due(now, CHASE_AT) < 0:
        return []

    from gmail_tools import list_unanswered_sent

    due = []
    for row in list_unanswered_sent(CHASE_QUIET_DAYS, CHASE_WINDOW_DAYS):
        if len(due) >= CHASE_PER_DAY:
            break
        # Nobody is ignoring him at no-reply@samsung.com.
        if triage.is_machine_address(row.get("recipient")):
            continue
        # Keyed on the message rather than the thread: if he writes into the
        # thread again and is met with silence again, that is a new fact and
        # worth raising once more.
        fingerprint = f"chase:{row['thread_id']}:{row['sent_at_ms']}"
        if not storage.claim(fingerprint, "chase"):
            continue
        due.append(Due(fingerprint, "chase", _chase_text(row, now), preclaimed=True))
    return due


# --- one question a day ---------------------------------------------------
#
# The only routine here that makes the assistant better rather than making Itai
# better informed. Everything it knows about his territory, his people and how
# he wants to be helped has to come from him, and it has never once asked -
# so once a day, at an hour when nothing else is competing for his attention,
# it asks exactly one question and remembers the answer forever.
#
# The rules that keep it from becoming a nuisance live in curiosity.py; the
# only one that matters here is that a question is asked once, ever. Silence is
# a legitimate answer and is not followed up.

QUESTION_AT = time(12, 30)


def _question_text(question) -> str:
    return "\n".join([
        "🧠 *שאלה אחת*",
        question.text,
        "",
        "(תענה כשנוח לך — אני אזכור את זה לתמיד ולא אשאל שוב. אם זה לא רלוונטי, תגיד לי ונתקדם.)",
    ])


def daily_question(now: datetime) -> list:
    """One thing the assistant does not know about Itai, asked out loud."""
    if now.weekday() not in WORK_DAYS:
        return []
    if _is_due(now, QUESTION_AT) < 0:
        return []

    # A day claim, taken before any question is chosen. Without it the three or
    # four ticks inside the grace window would each pick a different unasked
    # question and he would get his one question a day three times.
    day = f"question:day:{now:%Y-%m-%d}"
    if not storage.claim(day, "question"):
        return []

    import curiosity

    chosen = []

    def claim(key: str) -> bool:
        fingerprint = f"question:{key}"
        if storage.claim(fingerprint, "question"):
            chosen.append(fingerprint)
            return True
        return False

    try:
        question = curiosity.ask_next(claim)
    except Exception:
        storage.release(day)
        raise

    if question is None:
        # Nothing left to ask, and no model to invent one. The day claim stays:
        # asking again in twenty minutes would not produce a different answer.
        return []

    return [Due(
        chosen[-1],
        "question",
        _question_text(question),
        preclaimed=True,
        # If the message never goes out, the day goes back too - otherwise a
        # failed send today costs him the question and the day both.
        on_failed=lambda: storage.release(day),
    )]


def shared_files_mirror(now: datetime) -> list:
    """Keeps the working folder a live mirror of everything shared with the bot.

    Itai tracks what the bot can see by opening that folder in his own Drive,
    and a share he makes and never asks about would otherwise stay invisible
    there until somebody ran list_bot_shares. This sends nothing - the mirror
    is bookkeeping, not a message - so it always returns an empty list."""
    import drive_tools

    stats = drive_tools.mirror_bot_shares()
    if stats["added"] or stats["failed"]:
        logger.info(
            f"Drive mirror: {len(stats['added'])} shared files added to the "
            f"working folder, {len(stats['failed'])} could not be mirrored."
        )
    return []


ROUTINES = (attendance, reminders, unanswered_mail, new_mail, daily_question,
            shared_files_mirror)


# --- the tick ------------------------------------------------------------


def _recipient(now: datetime):
    """Returns (number, window_state).

    window_state is 'open' when Itai wrote within the last 24 hours, 'closed'
    when he did not, and 'unknown' when nothing has been recorded yet - a fresh
    database, or a deploy that predates last_inbound_at. Unknown is treated as
    worth attempting: a send Meta refuses costs nothing, while staying silent
    costs a reminder.
    """
    sender, wrote_at = storage.last_inbound() if storage.enabled() else (None, None)
    number = OWNER_PHONE or sender
    if wrote_at is None:
        return number, "unknown"
    return number, "open" if (now - wrote_at) < FREE_WINDOW else "closed"


def collect(now: datetime, routines=ROUTINES) -> list:
    """Asks every routine what is due. A routine that raises is skipped rather
    than allowed to take the tick down with it - one broken routine must not
    stop the attendance reminder."""
    items = []
    for routine in routines:
        try:
            items.extend(routine(now))
        except Exception:
            logger.exception(f"Proactive routine {routine.__name__} failed — skipped.")
    return items


def _run_hook(hook, fingerprint: str) -> None:
    """A routine's own bookkeeping must never be able to take down the tick -
    the message has already gone out by the time this runs, and raising here
    would lose the rest of the queue over an accounting problem."""
    if hook is None:
        return
    try:
        hook()
    except Exception as e:
        logger.error(f"Bookkeeping for {fingerprint} failed: {e}")


def _hold(items) -> list:
    """Nothing goes out this tick. Anything a routine claimed on its own has to
    go back, or an email claimed while the window was shut would be marked as
    told-about without ever having been told."""
    for item in items:
        if item.preclaimed:
            storage.release(item.fingerprint)
            _run_hook(item.on_failed, item.fingerprint)
    return [i.fingerprint for i in items]


def run_tick(send, now=None, routines=ROUTINES) -> dict:
    """One heartbeat. Returns a summary the caller can log or return as JSON.

    `send(to, text)` is passed in rather than imported so this module stays
    free of the Flask app that owns the WhatsApp credentials - and so a test
    can watch what would have gone out without a network call.
    """
    now = now or datetime.now(ISRAEL_TZ)
    summary = {"at": now.isoformat(), "due": 0, "sent": [], "held": [], "failed": []}

    items = collect(now, routines)
    summary["due"] = len(items)
    if not items:
        return summary

    number, window = _recipient(now)
    summary["window"] = window
    summary["recipient"] = "set" if number else "missing"

    if not number:
        # Nothing to send to. This only happens on a service that has never
        # received a message and has no OWNER_PHONE set.
        logger.error("Proactive tick has something to say and nobody to say it to.")
        summary["held"] = _hold(items)
        return summary

    if window == "closed":
        # Deliverable only as a paid template, which is out of scope by
        # instruction. Reported here so the reason is visible rather than
        # looking like the reminder simply never ran.
        logger.warning("24h WhatsApp window closed — %d proactive item(s) held.", len(items))
        summary["held"] = _hold(items)
        return summary

    for item in items:
        if not item.preclaimed and not storage.claim(item.fingerprint, item.kind):
            continue
        if send(number, item.text):
            summary["sent"].append(item.fingerprint)
            # Written into the conversation as a turn the assistant took, so
            # that "כן, תקרא" a minute later lands on a model that can see what
            # it just said. A proactive message missing from the history is one
            # the assistant has no memory of sending, and the reply to it
            # arrives as a non sequitur.
            storage.append_model_turn(number, item.text)
            _run_hook(item.on_sent, item.fingerprint)
        else:
            # The message never left. Give the claim back so the next tick can
            # try again while the item is still inside its grace window.
            storage.release(item.fingerprint)
            _run_hook(item.on_failed, item.fingerprint)
            summary["failed"].append(item.fingerprint)

    return summary
