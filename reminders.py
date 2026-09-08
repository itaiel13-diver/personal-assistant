"""Turning "תזכיר לי מחר בבוקר" into a row with a timestamp on it.

The parsing here is deliberately not the model's job alone. Gemini is given the
current Israel date and time and asked for an ISO timestamp, which it is good
at - but it is also the component that runs out of quota, hallucinates a year,
and occasionally returns the word "מחר" because that is what it was told. So
this module accepts either: a real timestamp, or the vague Hebrew phrase, and
resolves both to the same thing.

The vague-time defaults - morning 09:00, noon 14:00, evening 18:00, night 21:00
- are the convention every reminder bot converges on, and matter more than they
look. Without them "תזכיר לי בערב" either fails or fires at midnight, and a
reminder that arrives at the wrong hour is worse than one that never arrives:
it teaches Itai to stop trusting them.

Two rules are enforced whatever the input said. A reminder is never scheduled
in the past - a time that has already gone by today means tomorrow, because
nobody asks to be reminded of something ten minutes ago. And a recurrence
always advances past now, so a routine that was asleep for a week does not wake
up and fire seven copies of the same daily reminder.
"""

import re
from datetime import datetime, timedelta

from calendar_tools import ISRAEL_TZ

# The hour each vague phrase resolves to. Noon and afternoon share 14:00 on
# purpose: Itai is on the road at midday and "צהריים" in his messages has
# always meant the after-lunch stretch, not 12:00 sharp.
VAGUE_HOURS = {
    "בוקר": 9,
    "מוקדם": 7,
    "צהריים": 14,
    "צהרים": 14,
    "אחהצ": 14,
    "אחר הצהריים": 14,
    "ערב": 18,
    "לילה": 21,
    "לפנות ערב": 17,
}

RECURRENCES = ("once", "daily", "weekdays", "weekly", "monthly")

# What Itai actually writes, mapped to the recurrence it means. Longest first,
# because "כל יום ראשון" is weekly and must not match the "כל יום" rule.
RECURRENCE_WORDS = (
    ("כל יום ראשון", "weekly"),
    ("כל יום שני", "weekly"),
    ("כל יום שלישי", "weekly"),
    ("כל יום רביעי", "weekly"),
    ("כל יום חמישי", "weekly"),
    ("כל יום שישי", "weekly"),
    ("כל יום עבודה", "weekdays"),
    ("כל יום ה", "weekly"),
    ("ימי עבודה", "weekdays"),
    ("כל שבוע", "weekly"),
    ("כל שבועיים", "weekly"),
    ("כל חודש", "monthly"),
    ("כל יום", "daily"),
    ("יומי", "daily"),
    ("שבועי", "weekly"),
    ("חודשי", "monthly"),
    ("every day", "daily"),
    ("daily", "daily"),
    ("weekly", "weekly"),
    ("monthly", "monthly"),
    ("weekdays", "weekdays"),
)

# Sunday to Thursday, as Python counts weekdays. Same set the shift reminder
# uses - Israel's work week, not the calendar's.
WORK_DAYS = frozenset({6, 0, 1, 2, 3})

# Hebrew day names to Python's weekday numbering (Monday = 0).
DAY_NAMES = {
    "ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2,
    "חמישי": 3, "שישי": 4, "שבת": 5,
}

_HHMM = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
_RELATIVE = re.compile(r"עוד\s+(\d+)\s*(דקות|דקה|שעות|שעה|ימים|יום)")

# Hebrew says the small numbers as words far more often than as digits, and
# "עוד שעתיים" is one of the most natural ways to ask for a reminder there is.
# Longest first, so "חצי שעה" is not read as the word "שעה" on its own.
_WORDED_DELAYS = (
    ("רבע שעה", timedelta(minutes=15)),
    ("חצי שעה", timedelta(minutes=30)),
    ("שעתיים", timedelta(hours=2)),
    ("יומיים", timedelta(days=2)),
    ("שבועיים", timedelta(weeks=2)),
    ("שבוע", timedelta(weeks=1)),
)


def normalise_recurrence(phrase: str) -> str:
    """Reads a repeat out of free text, defaulting to a one-off.

    Defaulting to "once" is the safe direction: a reminder that should have
    repeated and did not gets asked for again, while one that repeats when it
    should not has to be hunted down and cancelled.
    """
    if not phrase:
        return "once"
    text = phrase.strip().lower()
    if text in RECURRENCES:
        return text
    for word, recurrence in RECURRENCE_WORDS:
        if word in text:
            return recurrence
    return "once"


def _apply_vague_hour(phrase: str, day: datetime) -> datetime | None:
    for word, hour in VAGUE_HOURS.items():
        if word in phrase:
            return day.replace(hour=hour, minute=0, second=0, microsecond=0)
    return None


def parse_when(phrase: str, now: datetime | None = None) -> datetime | None:
    """Resolves a due time, or returns None when the phrase says nothing about time.

    None means "ask him" - a reminder with a guessed time is a reminder that
    fires at the wrong moment, and this is the one place where refusing to
    guess is cheaper than guessing.
    """
    if not phrase:
        return None
    now = now or datetime.now(ISRAEL_TZ)
    text = phrase.strip()
    lowered = text.lower()

    # An ISO timestamp, which is what Gemini is asked for and usually returns.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ISRAEL_TZ)
        return parsed.astimezone(ISRAEL_TZ)
    except ValueError:
        pass

    if "עוד" in text or "בעוד" in text:
        for word, delta in _WORDED_DELAYS:
            if word in text:
                return now + delta

    relative = _RELATIVE.search(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("דק"):
            return now + timedelta(minutes=amount)
        if unit.startswith("שע"):
            return now + timedelta(hours=amount)
        return now + timedelta(days=amount)

    # Which day is being talked about, before the question of which hour.
    day = now
    if "מחרתיים" in text:
        day = now + timedelta(days=2)
    elif "מחר" in text:
        day = now + timedelta(days=1)
    elif "היום" in text or "עכשיו" in text:
        day = now
    else:
        for name, weekday in DAY_NAMES.items():
            if f"יום {name}" in text or f"ב{name}" in text:
                ahead = (weekday - now.weekday()) % 7 or 7
                day = now + timedelta(days=ahead)
                break

    clock = _HHMM.search(text)
    if clock:
        due = day.replace(hour=int(clock.group(1)), minute=int(clock.group(2)),
                          second=0, microsecond=0)
    else:
        due = _apply_vague_hour(text, day) or _apply_vague_hour(lowered, day)
        if due is None:
            # A bare day with no hour at all still deserves an answer, but only
            # when a day was actually named - otherwise there is nothing here.
            if day.date() == now.date():
                return None
            due = day.replace(hour=VAGUE_HOURS["בוקר"], minute=0, second=0, microsecond=0)

    # Nobody asks to be reminded of something that already happened. A time
    # that has passed today is meant for tomorrow.
    if due <= now:
        due += timedelta(days=1)
    return due


def next_occurrence(due_at: datetime, recurrence: str,
                    now: datetime | None = None) -> datetime | None:
    """Where a repeating reminder lands next, or None when it does not repeat.

    It advances until it is genuinely in the future rather than by one step, so
    a heartbeat that missed a week does not deliver seven days of backlog one
    tick at a time.
    """
    if recurrence not in RECURRENCES or recurrence == "once":
        return None
    now = now or datetime.now(ISRAEL_TZ)
    nxt = due_at
    # Bounded so a corrupt row cannot spin here forever; 400 steps covers more
    # than a year of daily reminders, which is far past the point of noticing.
    for _ in range(400):
        if recurrence == "daily":
            nxt += timedelta(days=1)
        elif recurrence == "weekdays":
            nxt += timedelta(days=1)
            while nxt.weekday() not in WORK_DAYS:
                nxt += timedelta(days=1)
        elif recurrence == "weekly":
            nxt += timedelta(days=7)
        elif recurrence == "monthly":
            month = nxt.month + 1
            year = nxt.year + (month > 12)
            month = month - 12 if month > 12 else month
            day = min(nxt.day, _days_in_month(year, month))
            nxt = nxt.replace(year=year, month=month, day=day)
        if nxt > now:
            return nxt
    return None


def _days_in_month(year: int, month: int) -> int:
    import calendar as _calendar

    return _calendar.monthrange(year, month)[1]


def describe(due_at: datetime, recurrence: str = "once") -> str:
    """How a reminder reads back to Itai when he asks what is scheduled."""
    when = due_at.astimezone(ISRAEL_TZ).strftime("%d/%m %H:%M")
    labels = {
        "daily": " (כל יום)",
        "weekdays": " (בימי עבודה)",
        "weekly": " (כל שבוע)",
        "monthly": " (כל חודש)",
    }
    return when + labels.get(recurrence, "")
