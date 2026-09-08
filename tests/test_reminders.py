"""The parsing half of the reminders: no database, no network, just time.

What is being protected here is that a reminder never fires at the wrong hour.
A missed reminder is annoying; one that arrives at 03:00, or on the wrong day,
or three times because a recurrence did not advance, is what makes someone stop
using reminders altogether.
"""
from datetime import datetime, timedelta

import pytest

import reminders
from reminders import ISRAEL_TZ


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ISRAEL_TZ)


# 2026-09-06 is a Sunday, 2026-09-10 a Thursday, 2026-09-11 a Friday.
SUNDAY = "2026-09-06"
THURSDAY = "2026-09-10"


# --- reading a time out of what he said ---------------------------------

def test_an_iso_timestamp_is_taken_as_given():
    """This is the normal path: Gemini knows the date and returns a timestamp."""
    due = reminders.parse_when("2026-09-09T09:00", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-09", "09:00")


def test_tomorrow_morning_is_nine_oclock():
    due = reminders.parse_when("מחר בבוקר", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-07", "09:00")


def test_tomorrow_evening_is_six():
    due = reminders.parse_when("מחר בערב", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-07", "18:00")


def test_this_evening_stays_today():
    due = reminders.parse_when("היום בערב", now=at(SUNDAY, "10:00"))
    assert due == at(SUNDAY, "18:00")


def test_an_explicit_clock_time_wins_over_the_vague_word():
    due = reminders.parse_when("מחר ב-14:30", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-07", "14:30")


def test_in_two_hours_is_two_hours_from_now():
    """Hebrew says its small numbers as words, and this is one of the most
    natural ways there is to ask for a reminder."""
    due = reminders.parse_when("עוד שעתיים", now=at(SUNDAY, "10:00"))
    assert due == at(SUNDAY, "12:00")


def test_in_half_an_hour_is_not_read_as_an_hour():
    due = reminders.parse_when("תזכיר לי עוד חצי שעה", now=at(SUNDAY, "10:00"))
    assert due == at(SUNDAY, "10:30")


def test_in_a_week_is_a_week():
    due = reminders.parse_when("עוד שבוע", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-13", "10:00")


def test_in_a_number_of_minutes_is_understood():
    due = reminders.parse_when("עוד 20 דקות", now=at(SUNDAY, "10:00"))
    assert due == at(SUNDAY, "10:20")


def test_in_a_number_of_hours_is_understood():
    due = reminders.parse_when("עוד 3 שעות", now=at(SUNDAY, "10:00"))
    assert due == at(SUNDAY, "13:00")


def test_a_named_weekday_lands_on_the_next_one():
    due = reminders.parse_when("ביום חמישי בבוקר", now=at(SUNDAY, "10:00"))
    assert due == at(THURSDAY, "09:00")


def test_a_named_weekday_that_is_today_means_next_week():
    """Asked on Sunday for "ראשון", he means the coming Sunday, not five
    minutes ago."""
    due = reminders.parse_when("ביום ראשון ב-9:00", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-13", "09:00")


def test_a_day_named_without_an_hour_defaults_to_the_morning():
    due = reminders.parse_when("מחר", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-07", "09:00")


def test_an_hour_that_has_already_passed_today_means_tomorrow():
    """Nobody asks to be reminded of something at nine when it is already ten."""
    due = reminders.parse_when("ב-9:00", now=at(SUNDAY, "10:00"))
    assert due == at("2026-09-07", "09:00")


def test_a_phrase_with_no_time_in_it_returns_none_rather_than_guessing():
    """A guessed hour is a reminder that fires at the wrong moment. Better to
    make the assistant ask."""
    assert reminders.parse_when("לשלוח את הדוח לדנה", now=at(SUNDAY, "10:00")) is None
    assert reminders.parse_when("", now=at(SUNDAY, "10:00")) is None


def test_a_naive_iso_timestamp_is_read_as_israel_time():
    """Gemini is asked for local time and does not always attach an offset."""
    due = reminders.parse_when("2026-09-09T09:00", now=at(SUNDAY, "10:00"))
    assert due.tzinfo is not None
    assert due.hour == 9


# --- how often it repeats -----------------------------------------------

def test_a_repeat_is_once_unless_he_said_otherwise():
    """The safe default: a missing repeat gets asked for again, an unwanted one
    has to be hunted down and cancelled."""
    assert reminders.normalise_recurrence("") == "once"
    assert reminders.normalise_recurrence("מחר בבוקר") == "once"


def test_every_day_is_daily():
    assert reminders.normalise_recurrence("כל יום ב-9") == "daily"


def test_every_named_day_is_weekly_not_daily():
    """The ordering trap: "כל יום חמישי" contains "כל יום" and is not daily."""
    assert reminders.normalise_recurrence("כל יום חמישי") == "weekly"


def test_working_days_are_their_own_recurrence():
    assert reminders.normalise_recurrence("כל יום עבודה") == "weekdays"


def test_the_canonical_names_pass_through():
    for name in ("once", "daily", "weekdays", "weekly", "monthly"):
        assert reminders.normalise_recurrence(name) == name


# --- where the next one lands -------------------------------------------

def test_a_one_off_has_no_next_occurrence():
    assert reminders.next_occurrence(at(SUNDAY, "09:00"), "once", now=at(SUNDAY, "09:01")) is None


def test_a_daily_reminder_moves_to_tomorrow():
    nxt = reminders.next_occurrence(at(SUNDAY, "09:00"), "daily", now=at(SUNDAY, "09:05"))
    assert nxt == at("2026-09-07", "09:00")


def test_a_weekly_reminder_moves_a_week():
    nxt = reminders.next_occurrence(at(SUNDAY, "09:00"), "weekly", now=at(SUNDAY, "09:05"))
    assert nxt == at("2026-09-13", "09:00")


def test_a_weekdays_reminder_skips_friday_and_saturday():
    nxt = reminders.next_occurrence(at(THURSDAY, "09:00"), "weekdays",
                                    now=at(THURSDAY, "09:05"))
    assert nxt == at("2026-09-13", "09:00")  # the following Sunday


def test_a_missed_week_does_not_produce_a_week_of_backlog():
    """An instance asleep for a week must wake up and deliver one reminder,
    not seven. next_occurrence advances past now, not by one step."""
    nxt = reminders.next_occurrence(at(SUNDAY, "09:00"), "daily",
                                    now=at("2026-09-13", "12:00"))
    assert nxt == at("2026-09-14", "09:00")


def test_a_monthly_reminder_on_the_31st_survives_a_short_month():
    nxt = reminders.next_occurrence(at("2026-01-31", "09:00"), "monthly",
                                    now=at("2026-01-31", "09:05"))
    assert nxt == at("2026-02-28", "09:00")


def test_describe_reads_the_time_back_with_its_repeat():
    """The confirmation quotes the stored time, not what he asked for, so a
    bad parse is caught now instead of at the wrong hour tomorrow."""
    text = reminders.describe(at(SUNDAY, "09:00"), "daily")
    assert "06/09 09:00" in text and "כל יום" in text
