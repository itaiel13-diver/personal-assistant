"""Tests for the 20:00 close-of-day message.

What is being protected: the message goes out once a day and not more (the
claim discipline every routine keeps), it never invents a day Itai did not
have (the model is told to work only from the conversation it is given), and
it still goes out when the model is unavailable - the scores and prices are
facts gathered without a model, and the questions are the standing ones he
named himself.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evening_summary as es
import proactive
from proactive import ISRAEL_TZ


# 2026-09-08 is a Tuesday.
def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-08T{clock}").replace(tzinfo=ISRAEL_TZ)


# --- data sources -------------------------------------------------------------

FOOTBALL_PAYLOAD = {
    "events": [
        {"strLeague": "English Premier League", "strHomeTeam": "Arsenal",
         "strAwayTeam": "Chelsea", "intHomeScore": "2", "intAwayScore": "1"},
        {"strLeague": "Israeli Premier League", "strHomeTeam": "מכבי תל אביב",
         "strAwayTeam": "הפועל באר שבע", "intHomeScore": "1", "intAwayScore": "0"},
        {"strLeague": "Israeli Premier League", "strHomeTeam": "מכבי חיפה",
         "strAwayTeam": "הפועל תל אביב", "intHomeScore": None, "intAwayScore": None},
    ]
}


def _json_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_football_puts_israel_first_and_skips_unfinished_matches():
    with patch.object(es.requests, "get", return_value=_json_response(FOOTBALL_PAYLOAD)):
        lines = es.fetch_football("2026-09-08")
    assert lines[0].startswith("מכבי תל אביב 1-0")
    assert "Arsenal 2-1 Chelsea" in lines[1]
    assert all("מכבי חיפה" not in line for line in lines)


def test_football_failure_returns_empty_instead_of_raising():
    with patch.object(es.requests, "get", side_effect=RuntimeError("down")):
        assert es.fetch_football("2026-09-08") == []


STOCKS_CSV = (
    "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
    "NVDA.US,2026-09-08,21:59:00,170,176,169,175.1,123456\n"
    "FAKE.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
)


def test_stocks_show_the_close_and_the_days_move():
    resp = MagicMock()
    resp.text = STOCKS_CSV
    resp.raise_for_status.return_value = None
    with patch.object(es.requests, "get", return_value=resp):
        lines = es.fetch_stocks(["NVDA.US", "FAKE.US"])
    assert lines[0].startswith("NVDA.US: 175.1 (+3.0%")
    assert "אין נתון זמין" in lines[1]


def test_an_empty_watchlist_fetches_nothing():
    with patch.object(es.requests, "get") as get:
        assert es.fetch_stocks([]) == []
    get.assert_not_called()


# --- the message --------------------------------------------------------------

def test_the_model_path_hands_the_model_the_day_and_returns_its_text():
    captured = {}

    def fake_ask(prompt, system="", max_tokens=600, temperature=0.2, skip=()):
        captured["prompt"] = prompt
        return "🌙 סיכום מהמודל"

    with patch.object(es.llm, "ask", side_effect=fake_ask), \
         patch.object(es, "fetch_football", return_value=["מכבי 1-0"]), \
         patch.object(es, "fetch_stocks", return_value=["NVDA.US: 175.1"]), \
         patch.object(es, "_todays_conversation", return_value="איתי: הייתי ביבנה"):
        text = es.build_message(at("20:00"), "972500000000")
    assert text == "🌙 סיכום מהמודל"
    assert "מכבי 1-0" in captured["prompt"]
    assert "NVDA.US" in captured["prompt"]
    assert "הייתי ביבנה" in captured["prompt"]


def test_without_a_model_the_message_still_goes_out_with_real_data():
    with patch.object(es.llm, "ask", return_value=None), \
         patch.object(es, "fetch_football", return_value=["מכבי 1-0"]), \
         patch.object(es, "fetch_stocks", return_value=[]), \
         patch.object(es, "_todays_conversation", return_value=""):
        text = es.build_message(at("20:00"), "972500000000")
    assert "🌙" in text and "⚽" in text and "📈" in text and "❓" in text
    assert "מכבי 1-0" in text
    assert "VOC" in text  # his own standing question survives the fallback


def test_the_fallback_asks_for_holdings_instead_of_inventing_prices():
    """Excellence has no retail API and no watchlist is configured: the honest
    section is a question, not a made-up number."""
    with patch.object(es.llm, "ask", return_value=None), \
         patch.object(es, "fetch_football", return_value=[]), \
         patch.object(es, "fetch_stocks", return_value=[]), \
         patch.object(es, "_todays_conversation", return_value=""):
        text = es.build_message(at("20:00"), "972500000000")
    assert "לא הוגדרו מניות" in text


def test_a_broken_model_call_falls_back_instead_of_raising():
    with patch.object(es.llm, "ask", side_effect=RuntimeError("boom")), \
         patch.object(es, "fetch_football", return_value=[]), \
         patch.object(es, "fetch_stocks", return_value=[]), \
         patch.object(es, "_todays_conversation", return_value=""):
        text = es.build_message(at("20:00"), "972500000000")
    assert "🌙" in text


# --- the routine's once-a-day discipline --------------------------------------

class _Ledger:
    def __init__(self, claim_ok=True):
        self.claimed = []
        self.released = []
        self.claim_ok = claim_ok

    def claim(self, fingerprint, kind):
        self.claimed.append(fingerprint)
        return self.claim_ok

    def release(self, fingerprint):
        self.released.append(fingerprint)

    def enabled(self):
        return True

    def last_inbound(self):
        return ("972500000000", None)


def test_the_summary_fires_in_its_window_and_claims_the_day(monkeypatch):
    ledger = _Ledger()
    monkeypatch.setattr(proactive.storage, "claim", ledger.claim)
    monkeypatch.setattr(proactive.storage, "release", ledger.release)
    monkeypatch.setattr(proactive.storage, "enabled", ledger.enabled)
    monkeypatch.setattr(proactive.storage, "last_inbound", ledger.last_inbound)
    with patch("evening_summary.build_message", return_value="🌙 ערב טוב"):
        due = proactive.evening_summary(at("20:05"))
    assert [d.kind for d in due] == ["evening-summary"]
    assert ledger.claimed == ["evening:2026-09-08"]
    assert due[0].preclaimed is True


def test_the_summary_stays_quiet_outside_its_window(monkeypatch):
    ledger = _Ledger()
    monkeypatch.setattr(proactive.storage, "claim", ledger.claim)
    with patch("evening_summary.build_message") as build:
        assert proactive.evening_summary(at("19:00")) == []
        assert proactive.evening_summary(at("21:30")) == []  # past the grace
    build.assert_not_called()
    assert ledger.claimed == []


def test_a_second_tick_the_same_evening_says_nothing(monkeypatch):
    monkeypatch.setattr(proactive.storage, "claim", _Ledger(claim_ok=False).claim)
    with patch("evening_summary.build_message") as build:
        assert proactive.evening_summary(at("20:05")) == []
    build.assert_not_called()


def test_a_build_that_blows_up_gives_the_day_back(monkeypatch):
    """A transient failure (network, quota) must not cost him the whole
    evening: the claim is released so the next tick tries again."""
    ledger = _Ledger()
    monkeypatch.setattr(proactive.storage, "claim", ledger.claim)
    monkeypatch.setattr(proactive.storage, "release", ledger.release)
    monkeypatch.setattr(proactive.storage, "enabled", ledger.enabled)
    monkeypatch.setattr(proactive.storage, "last_inbound", ledger.last_inbound)
    with patch("evening_summary.build_message", side_effect=RuntimeError("x")):
        try:
            proactive.evening_summary(at("20:05"))
            assert False, "the routine should propagate so collect() logs it"
        except RuntimeError:
            pass
    assert ledger.released == ["evening:2026-09-08"]


def test_the_routine_is_registered_with_the_heartbeat():
    assert proactive.evening_summary in proactive.ROUTINES
