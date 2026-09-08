"""Tests for the active half of the assistant.

Two things are being protected here. The first is that the attendance reminder
actually fires - it is the highest-priority routine in the project and the one
whose absence costs Itai money. The second is that nothing is ever said twice:
the heartbeat is an external pinger with no delivery guarantee, so a routine
gets called repeatedly for the same window by design, and only the claim in the
database stands between that and an assistant that nags.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proactive
from proactive import ISRAEL_TZ, Due


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ISRAEL_TZ)


# 2026-09-06 is a Sunday, 2026-09-11 a Friday, 2026-09-12 a Saturday.
SUNDAY = "2026-09-06"
FRIDAY = "2026-09-11"
SATURDAY = "2026-09-12"


class FakeLedger:
    """Stands in for the database: remembers what was claimed, nothing else."""

    # "he wrote an hour ago" is the ordinary case, so it is the default. None
    # has to stay meaningful - it is the never-wrote case the tick treats as an
    # unknown window - hence a sentinel rather than None as the default.
    _DEFAULT = object()

    def __init__(self, sender="972500000000", wrote_at=_DEFAULT, now=None):
        self.claimed = set()
        self.released = []
        self.turns = []
        self.reminders = []
        self.unclaimed = []
        self.rescheduled = []
        self.sender = sender
        if wrote_at is self._DEFAULT:
            wrote_at = now - timedelta(hours=1) if now else None
        self.wrote_at = wrote_at

    def enabled(self):
        return True

    def claim(self, fingerprint, kind):
        if fingerprint in self.claimed:
            return False
        self.claimed.add(fingerprint)
        return True

    def release(self, fingerprint):
        self.claimed.discard(fingerprint)
        self.released.append(fingerprint)

    def last_inbound(self):
        return (self.sender, self.wrote_at)

    def append_model_turn(self, sender_id, text):
        self.turns.append((sender_id, text))

    # The reminder rows behave like the real table: claiming one flips its
    # status in the same breath as reading it, which is what stops two ticks
    # delivering the same reminder.
    def claim_due_reminders(self, now, limit=5):
        ready = [r for r in self.reminders
                 if r["status"] == "pending" and r["due_at"] <= now][:limit]
        for row in ready:
            row["status"] = "sent"
        return [dict(r) for r in ready]

    def unclaim_reminder(self, reminder_id):
        self.unclaimed.append(reminder_id)
        for row in self.reminders:
            if row["id"] == reminder_id:
                row["status"] = "pending"

    def reschedule_reminder(self, reminder_id, next_due):
        self.rescheduled.append((reminder_id, next_due))
        for row in self.reminders:
            if row["id"] == reminder_id:
                row["due_at"] = next_due
                row["status"] = "pending"


class Outbox:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, to, text):
        self.sent.append((to, text))
        return self.ok


@pytest.fixture
def ledger(monkeypatch):
    def install(now=None, **kwargs):
        fake = FakeLedger(now=now, **kwargs)
        for name in ("enabled", "claim", "release", "last_inbound",
                     "append_model_turn", "claim_due_reminders",
                     "unclaim_reminder", "reschedule_reminder"):
            monkeypatch.setattr(proactive.storage, name, getattr(fake, name))
        return fake
    return install


@pytest.fixture(autouse=True)
def inbox(monkeypatch):
    """Every test gets an empty mailbox unless it asks for one with mail in it.

    This is autouse on purpose: new_mail is in the default ROUTINES, so without
    it a test of the attendance reminder would reach out to Gmail over the
    network to find out that it has nothing to say.
    """
    import gmail_tools

    box = []

    def fake_list(query="", max_results=10):
        return box[:max_results]

    monkeypatch.setattr(gmail_tools, "list_inbox_messages", fake_list)

    def fill(*messages):
        box.clear()
        box.extend(messages)
        return box
    return fill


def mail(id, sender="dana@impact.co.il", subject="נושא", snippet="גוף ההודעה"):
    return {"id": id, "sender": sender, "subject": subject,
            "snippet": snippet, "date": ""}


# --- the attendance routine ---------------------------------------------


def test_the_clock_in_reminder_fires_at_its_minute():
    due = proactive.attendance(at(SUNDAY, "08:55"))
    assert len(due) == 1
    assert due[0].kind == "attendance"
    assert due[0].fingerprint == f"attendance:in:{SUNDAY}"
    assert "כניסה" in due[0].text
    assert "Connecteam" in due[0].text


def test_the_clock_out_reminder_fires_at_its_minute():
    due = proactive.attendance(at(SUNDAY, "17:55"))
    assert [d.fingerprint for d in due] == [f"attendance:out:{SUNDAY}"]
    assert "יציאה" in due[0].text


def test_nothing_fires_before_the_slot():
    assert proactive.attendance(at(SUNDAY, "08:54")) == []


def test_a_late_tick_still_fires_and_says_how_late():
    # The pinger is a free service with no minute-level guarantee, and a cold
    # instance takes time to wake. A reminder twenty minutes late is still the
    # difference between a registered shift and an unregistered one.
    due = proactive.attendance(at(SUNDAY, "09:15"))
    assert len(due) == 1
    assert "20" in due[0].text


def test_a_tick_after_the_grace_has_passed_says_nothing():
    # By late morning the reminder is no longer help, it is noise.
    assert proactive.attendance(at(SUNDAY, "10:30")) == []


def test_no_attendance_reminders_on_friday_or_saturday():
    for day in (FRIDAY, SATURDAY):
        assert proactive.attendance(at(day, "08:55")) == []
        assert proactive.attendance(at(day, "17:55")) == []


def test_the_fingerprint_is_per_day_so_tomorrow_is_a_new_reminder():
    today = proactive.attendance(at(SUNDAY, "08:55"))[0]
    tomorrow = proactive.attendance(at("2026-09-07", "08:55"))[0]
    assert today.fingerprint != tomorrow.fingerprint


# --- the tick ------------------------------------------------------------


def test_a_due_item_is_sent_once_and_never_again(ledger):
    now = at(SUNDAY, "08:55")
    ledger(now=now)
    out = Outbox()

    first = proactive.run_tick(send=out, now=now)
    assert first["sent"] == [f"attendance:in:{SUNDAY}"]

    # The pinger calls again four minutes later, inside the same grace window.
    second = proactive.run_tick(send=out, now=at(SUNDAY, "08:59"))
    assert second["sent"] == []
    assert len(out.sent) == 1


def test_a_quiet_tick_sends_nothing_and_touches_nothing(ledger):
    now = at(SUNDAY, "11:20")
    fake = ledger(now=now)
    out = Outbox()
    summary = proactive.run_tick(send=out, now=now)
    assert summary["due"] == 0
    assert out.sent == []
    assert fake.claimed == set()


def test_a_send_that_fails_is_released_so_the_next_tick_retries(ledger):
    now = at(SUNDAY, "08:55")
    fake = ledger(now=now)
    failing = Outbox(ok=False)

    summary = proactive.run_tick(send=failing, now=now)
    assert summary["failed"] == [f"attendance:in:{SUNDAY}"]
    assert fake.released == [f"attendance:in:{SUNDAY}"]

    # Nothing is left claimed, so the retry a few minutes later goes out.
    working = Outbox()
    retry = proactive.run_tick(send=working, now=at(SUNDAY, "09:00"))
    assert retry["sent"] == [f"attendance:in:{SUNDAY}"]


def test_nothing_is_sent_outside_the_free_24_hour_window(ledger):
    # WhatsApp will not carry a free-form business message more than 24 hours
    # after Itai's own last one, and a paid template is out of scope. The item
    # is held and reported rather than silently dropped - and, critically, it
    # is not claimed, so it can still go out if he writes inside the grace.
    now = at(SUNDAY, "08:55")
    fake = ledger(now=now, wrote_at=now - timedelta(hours=30))
    out = Outbox()

    summary = proactive.run_tick(send=out, now=now)
    assert summary["window"] == "closed"
    assert summary["held"] == [f"attendance:in:{SUNDAY}"]
    assert out.sent == []
    assert fake.claimed == set()


def test_an_unknown_window_is_attempted_rather_than_assumed_shut(ledger):
    # A fresh database has no record of him ever writing. Meta refusing a send
    # costs nothing; staying silent costs the reminder.
    now = at(SUNDAY, "08:55")
    ledger(now=now, wrote_at=None)
    out = Outbox()
    summary = proactive.run_tick(send=out, now=now)
    assert summary["window"] == "unknown"
    assert len(out.sent) == 1


def test_with_no_recipient_at_all_nothing_is_claimed(ledger):
    now = at(SUNDAY, "08:55")
    fake = ledger(now=now, sender=None, wrote_at=None)
    monkey_free = Outbox()
    summary = proactive.run_tick(send=monkey_free, now=now)
    assert summary["recipient"] == "missing"
    assert monkey_free.sent == []
    assert fake.claimed == set()


def test_the_owner_phone_env_var_wins_over_the_last_sender(ledger, monkeypatch):
    now = at(SUNDAY, "08:55")
    ledger(now=now, sender="972511111111")
    monkeypatch.setattr(proactive, "OWNER_PHONE", "972522222222")
    out = Outbox()
    proactive.run_tick(send=out, now=now)
    assert out.sent[0][0] == "972522222222"


def test_one_broken_routine_does_not_stop_the_attendance_reminder(ledger):
    now = at(SUNDAY, "08:55")
    ledger(now=now)

    def broken(_now):
        raise RuntimeError("this routine is having a bad day")

    out = Outbox()
    summary = proactive.run_tick(send=out, now=now,
                                 routines=(broken, proactive.attendance))
    assert summary["sent"] == [f"attendance:in:{SUNDAY}"]


def test_every_routine_produces_items_the_ledger_can_tell_apart(ledger):
    # A routine whose fingerprints collide with another's would silence one of
    # them at random, and the failure would look like a scheduling bug.
    now = at(SUNDAY, "08:55")
    ledger(now=now)
    seen = set()
    for routine in proactive.ROUTINES:
        for item in routine(now):
            assert isinstance(item, Due)
            assert item.fingerprint not in seen
            seen.add(item.fingerprint)


# --- the mail watch ------------------------------------------------------


def test_a_new_email_is_raised_once_and_then_never_again(ledger, inbox):
    now = at(SUNDAY, "11:00")
    ledger(now=now)
    inbox(mail("18f", sender="dana@impact.co.il", subject="לוח משמרות"))

    first = proactive.new_mail(now)
    assert len(first) == 1
    assert first[0].fingerprint == "mail:18f"
    assert "dana@impact.co.il" in first[0].text
    assert "לוח משמרות" in first[0].text
    # The id travels in the message so his "כן" has something to act on.
    assert "[id:18f]" in first[0].text

    # Same mail still unread on the next tick five minutes later.
    assert proactive.new_mail(at(SUNDAY, "11:05")) == []


def test_the_mail_watch_claims_before_it_decides(ledger, inbox):
    # A mail item is claimed inside the routine rather than by the tick, so it
    # arrives already claimed and the tick must not claim it a second time.
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    inbox(mail("aa"))
    due = proactive.new_mail(now)
    assert due[0].preclaimed is True
    assert fake.claimed == {"mail:aa"}


def test_mail_held_by_a_shut_window_is_released_not_swallowed(ledger, inbox):
    # The dangerous case: claimed by the routine, then never sent. Without the
    # release the email is marked told-about while Itai has heard nothing.
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now, wrote_at=now - timedelta(hours=30))
    inbox(mail("bb"))

    summary = proactive.run_tick(send=Outbox(), now=now)
    assert summary["held"] == ["mail:bb"]
    assert fake.released == ["mail:bb"]
    assert fake.claimed == set()

    # He writes; the window reopens; the email still goes out.
    later = at(SUNDAY, "11:30")
    fake.wrote_at = later
    out = Outbox()
    proactive.run_tick(send=out, now=later)
    assert len(out.sent) == 1


def test_a_burst_of_mail_is_spread_over_ticks_rather_than_dumped(ledger, inbox):
    now = at(SUNDAY, "11:00")
    ledger(now=now)
    inbox(*[mail(f"m{i}") for i in range(8)])

    first = proactive.new_mail(now)
    assert len(first) == proactive.MAIL_PER_TICK
    # The rest are not lost - they are simply still unread on the next tick.
    second = proactive.new_mail(at(SUNDAY, "11:05"))
    assert len(second) == 8 - proactive.MAIL_PER_TICK
    assert not {i.fingerprint for i in first} & {i.fingerprint for i in second}


def test_the_mail_watch_is_quiet_at_night(ledger, inbox):
    ledger(now=at(SUNDAY, "03:00"))
    inbox(mail("cc"))
    assert proactive.new_mail(at(SUNDAY, "03:00")) == []
    assert proactive.new_mail(at(SUNDAY, "23:40")) == []
    # And speaks again in the morning - the email was not consumed overnight.
    assert len(proactive.new_mail(at(SUNDAY, "07:30"))) == 1


def test_the_mail_watch_works_on_days_the_attendance_reminder_does_not(ledger, inbox):
    # Mail is not tied to the work week; Friday is a working day for the inbox.
    now = at(FRIDAY, "11:00")
    ledger(now=now)
    inbox(mail("dd"))
    out = Outbox()
    summary = proactive.run_tick(send=out, now=now)
    assert summary["sent"] == ["mail:dd"]


def test_a_proactive_message_is_written_into_the_conversation(ledger, inbox):
    # Otherwise his reply lands on a model with no record of the question.
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    inbox(mail("ee"))
    out = Outbox()
    proactive.run_tick(send=out, now=now)
    assert len(fake.turns) == 1
    sender, text = fake.turns[0]
    assert sender == "972500000000"
    assert text == out.sent[0][1]


def test_a_message_that_never_left_is_not_written_into_the_conversation(ledger, inbox):
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    inbox(mail("ff"))
    proactive.run_tick(send=Outbox(ok=False), now=now)
    assert fake.turns == []


def test_gmail_falling_over_does_not_stop_the_attendance_reminder(ledger, monkeypatch):
    now = at(SUNDAY, "08:55")
    ledger(now=now)
    import gmail_tools

    def broken(query="", max_results=10):
        raise RuntimeError("Gmail is having a bad day")

    monkeypatch.setattr(gmail_tools, "list_inbox_messages", broken)
    out = Outbox()
    summary = proactive.run_tick(send=out, now=now)
    assert summary["sent"] == [f"attendance:in:{SUNDAY}"]


# --- reminders ----------------------------------------------------------
#
# These are the messages Itai asked for by name, which makes them the ones he
# notices most: a duplicate reads as a broken assistant, and a repeat that
# stops repeating reads as one that forgot.

def reminder(id=1, text="לשלוח את הדוח לדנה", due="09:00", day=SUNDAY,
             recurrence="once", status="pending"):
    return {"id": id, "sender_id": "972500000000", "text": text,
            "due_at": at(day, due), "recurrence": recurrence, "status": status}


def test_a_due_reminder_is_delivered(ledger):
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    fake.reminders.append(reminder())
    out = Outbox()

    summary = proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(out.sent) == 1
    assert "לשלוח את הדוח לדנה" in out.sent[0][1]
    assert len(summary["sent"]) == 1


def test_a_reminder_that_is_not_due_yet_stays_quiet(ledger):
    now = at(SUNDAY, "08:00")
    fake = ledger(now=now)
    fake.reminders.append(reminder(due="09:00"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert out.sent == []


def test_a_reminder_is_never_delivered_twice(ledger):
    """The claim and the read are one statement, so a second tick over the same
    window finds nothing left to take."""
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    fake.reminders.append(reminder())
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    proactive.run_tick(out, now=now + timedelta(minutes=5), routines=(proactive.reminders,))
    assert len(out.sent) == 1


def test_a_late_reminder_says_which_minute_it_was_for(ledger):
    """Ticks are irregular. Without the original time a late reminder is just
    a confusing one."""
    now = at(SUNDAY, "09:40")
    fake = ledger(now=now)
    fake.reminders.append(reminder(due="09:00"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert "09:00" in out.sent[0][1]


def test_a_punctual_reminder_does_not_bother_quoting_the_time(ledger):
    now = at(SUNDAY, "09:02")
    fake = ledger(now=now)
    fake.reminders.append(reminder(due="09:00"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert "נקבעה" not in out.sent[0][1]


def test_a_daily_reminder_is_rearmed_for_tomorrow(ledger):
    now = at(SUNDAY, "09:05")
    fake = ledger(now=now)
    fake.reminders.append(reminder(recurrence="daily"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(out.sent) == 1
    assert fake.rescheduled == [(1, at("2026-09-07", "09:00"))]


def test_a_one_off_reminder_is_not_rearmed(ledger):
    now = at(SUNDAY, "09:05")
    fake = ledger(now=now)
    fake.reminders.append(reminder(recurrence="once"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert fake.rescheduled == []


def test_a_repeat_is_only_rearmed_once_the_message_actually_went_out(ledger):
    """Advancing a recurrence on a failed send would skip that occurrence
    entirely - the one way a repeating reminder silently loses a day."""
    now = at(SUNDAY, "09:05")
    fake = ledger(now=now)
    fake.reminders.append(reminder(recurrence="daily"))
    out = Outbox(ok=False)

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert fake.rescheduled == []
    assert fake.unclaimed == [1]


def test_a_failed_send_leaves_the_reminder_pending_for_the_next_tick(ledger):
    now = at(SUNDAY, "09:05")
    fake = ledger(now=now)
    fake.reminders.append(reminder())

    proactive.run_tick(Outbox(ok=False), now=now, routines=(proactive.reminders,))
    assert fake.reminders[0]["status"] == "pending"

    out = Outbox()
    proactive.run_tick(out, now=now + timedelta(minutes=30), routines=(proactive.reminders,))
    assert len(out.sent) == 1


def test_a_reminder_held_by_a_shut_window_is_put_back_not_lost(ledger):
    """Outside WhatsApp's 24-hour window nothing can be delivered. The reminder
    has to survive that and go out when he next writes."""
    now = at(SUNDAY, "09:05")
    fake = ledger(now=now, wrote_at=now - timedelta(hours=30))
    fake.reminders.append(reminder())
    out = Outbox()

    summary = proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert out.sent == []
    assert summary["held"]
    assert fake.reminders[0]["status"] == "pending"

    fake.wrote_at = now
    proactive.run_tick(out, now=now + timedelta(minutes=10), routines=(proactive.reminders,))
    assert len(out.sent) == 1


def test_a_backlog_is_spread_over_ticks_rather_than_dumped_at_once(ledger):
    """An instance asleep over a weekend comes back to a pile. Fifteen messages
    at once is not catching up."""
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    for i in range(1, 9):
        fake.reminders.append(reminder(id=i, due="09:00", text=f"פריט {i}"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(out.sent) == proactive.REMINDERS_PER_TICK

    proactive.run_tick(out, now=now + timedelta(minutes=30), routines=(proactive.reminders,))
    assert len(out.sent) == 8


def test_reminders_fire_at_night_because_he_asked_for_them(ledger):
    """The mail watch is quiet at night because nobody asked for that mail.
    None of that reasoning applies to a time he set himself."""
    now = at(SUNDAY, "05:30")
    fake = ledger(now=now)
    fake.reminders.append(reminder(due="05:30"))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(out.sent) == 1


def test_reminders_fire_on_friday_when_the_shift_reminder_does_not(ledger):
    now = at(FRIDAY, "09:05")
    fake = ledger(now=now)
    fake.reminders.append(reminder(day=FRIDAY))
    out = Outbox()

    proactive.run_tick(out, now=now, routines=proactive.ROUTINES)
    assert len(out.sent) == 1
    assert "תזכורת" in out.sent[0][1]


def test_a_reminder_is_written_into_the_conversation(ledger):
    """So that "טופל" a minute later lands on a model that can see what it
    just said."""
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    fake.reminders.append(reminder())
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(fake.turns) == 1


def test_a_broken_reschedule_does_not_lose_the_rest_of_the_queue(ledger):
    """The message has already gone out by the time the bookkeeping runs.
    Raising there would cost the reminders behind it."""
    now = at(SUNDAY, "11:00")
    fake = ledger(now=now)
    fake.reminders.append(reminder(id=1, recurrence="daily", text="ראשון"))
    fake.reminders.append(reminder(id=2, text="שני"))

    def explode(reminder_id, next_due):
        raise RuntimeError("database went away")

    import proactive as p
    p.storage.reschedule_reminder = explode
    out = Outbox()

    proactive.run_tick(out, now=now, routines=(proactive.reminders,))
    assert len(out.sent) == 2
