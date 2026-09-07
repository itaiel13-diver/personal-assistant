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
        for name in ("enabled", "claim", "release", "last_inbound"):
            monkeypatch.setattr(proactive.storage, name, getattr(fake, name))
        return fake
    return install


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
