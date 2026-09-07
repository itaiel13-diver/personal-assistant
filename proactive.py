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
    """
    fingerprint: str
    kind: str
    text: str


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


ROUTINES = (attendance,)


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
        summary["held"] = [i.fingerprint for i in items]
        return summary

    if window == "closed":
        # Deliverable only as a paid template, which is out of scope by
        # instruction. Reported here so the reason is visible rather than
        # looking like the reminder simply never ran.
        logger.warning("24h WhatsApp window closed — %d proactive item(s) held.", len(items))
        summary["held"] = [i.fingerprint for i in items]
        return summary

    for item in items:
        if not storage.claim(item.fingerprint, item.kind):
            continue
        if send(number, item.text):
            summary["sent"].append(item.fingerprint)
        else:
            # The message never left. Give the claim back so the next tick can
            # try again while the item is still inside its grace window.
            storage.release(item.fingerprint)
            summary["failed"].append(item.fingerprint)

    return summary
