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

def _patched_data(ask, football=None, fixtures=None, stocks=None, history=""):
    """The standard fixture set: every data source patched, so a test names
    only the one it cares about."""
    return (
        patch.object(es.llm, "ask", side_effect=ask) if ask else
        patch.object(es.llm, "ask", return_value=None),
        patch.object(es, "fetch_football", return_value=football or []),
        patch.object(es, "fetch_fixtures", return_value=fixtures or []),
        patch.object(es, "stocks_section", return_value=stocks or []),
        patch.object(es, "_todays_conversation", return_value=history),
    )


def test_the_model_writes_only_the_two_judgement_sections():
    """After the 2026-09-08 hallucination the model is never handed the feeds:
    it writes the recap and the questions from the conversation, and the
    data sections are composed in code around its text."""
    captured = {}

    def fake_ask(prompt, system="", max_tokens=600, temperature=0.2, skip=()):
        captured["prompt"] = prompt
        return "[סיכום]\n- היה ביבנה\n[שאלות]\n- עשית VOC?"

    patches = _patched_data(fake_ask, football=["מכבי 1-0"],
                            fixtures=["יום רביעי 21:00: X נגד Y"],
                            stocks=["NVDA.US: 175.1"], history="איתי: הייתי ביבנה")
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        text = es.build_message(at("20:00"), "972500000000")
    assert "היה ביבנה" in text and "עשית VOC" in text
    assert "מכבי 1-0" in text and "NVDA.US: 175.1" in text
    assert "יום רביעי 21:00" in text
    assert "הייתי ביבנה" in captured["prompt"]
    assert "מכבי 1-0" not in captured["prompt"]  # the model never sees the feed
    assert "NVDA.US" not in captured["prompt"]


def test_the_model_cannot_add_a_match_the_feed_did_not_return():
    """Regression for the first summary, which announced a 2026 World Cup
    final from the model's own memory: the football and fixture sections are
    built from the feed alone, so a hallucinating model cannot put a game in
    them even when it tries."""
    def hallucinating_ask(prompt, **kwargs):
        return "[סיכום]\n- גמר מונדיאל 2026 היום ב-22:00!\n[שאלות]\n- צפית?"

    patches = _patched_data(hallucinating_ask)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        text = es.build_message(at("20:00"), "972500000000")
    football = text.split("⚽ *כדורגל*")[1].split("🗓️")[0]
    fixtures = text.split("🗓️ *משחקים קרובים*")[1].split("📈")[0]
    assert "אין תוצאות" in football and "מונדיאל" not in football
    assert "אין משחקים קרובים" in fixtures and "מונדיאל" not in fixtures


def test_without_a_model_the_message_still_goes_out_with_real_data():
    patches = _patched_data(None, football=["מכבי 1-0"])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        text = es.build_message(at("20:00"), "972500000000")
    assert "🌙" in text and "⚽" in text and "📈" in text and "❓" in text
    assert "מכבי 1-0" in text
    assert "VOC" in text  # his own standing question survives the fallback


def test_the_fallback_asks_for_holdings_instead_of_inventing_prices():
    """Excellence has no retail API and no watchlist is configured: the honest
    section is a question, not a made-up number."""
    patches = _patched_data(None)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        text = es.build_message(at("20:00"), "972500000000")
    assert "לא הוגדרו מניות" in text


def test_a_broken_model_call_falls_back_instead_of_raising():
    patches = _patched_data(None)
    with patch.object(es.llm, "ask", side_effect=RuntimeError("boom")), \
         patches[1], patches[2], patches[3], patches[4]:
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


# --- fixtures ----------------------------------------------------------------

def test_fixtures_come_from_the_feed_israel_first():
    payload = {"events": [
        {"strLeague": "Spanish La Liga", "strHomeTeam": "Real Madrid",
         "strAwayTeam": "Barcelona", "intHomeScore": None, "intAwayScore": None,
         "strTimestamp": "2026-09-09T18:00:00"},
        {"strLeague": "Israeli Premier League", "strHomeTeam": "מכבי תל אביב",
         "strAwayTeam": "הפועל חיפה", "intHomeScore": None, "intAwayScore": None,
         "strTimestamp": "2026-09-09T17:00:00"},
        {"strLeague": "English Premier League", "strHomeTeam": "Arsenal",
         "strAwayTeam": "Chelsea", "intHomeScore": "2", "intAwayScore": "1"},
    ]}
    with patch.object(es.requests, "get", return_value=_json_response(payload)):
        lines = es.fetch_fixtures("2026-09-08", days_ahead=1)
    assert len(lines) == 2  # the finished match is not a fixture
    assert lines[0].startswith("יום רביעי 09/09 20:00: מכבי תל אביב נגד")
    assert "Real Madrid נגד Barcelona" in lines[1]


def test_fixtures_failure_returns_empty_instead_of_raising():
    with patch.object(es.requests, "get", side_effect=RuntimeError("down")):
        assert es.fetch_fixtures("2026-09-08") == []


# --- the portfolio in the stocks section -------------------------------------

def test_a_stored_portfolio_replaces_the_watchlist():
    import portfolio
    holdings = [{"name": "NVIDIA", "symbol": "NVDA", "quantity": 10, "cost": 150.0}]
    with patch.object(portfolio, "load_holdings", return_value=holdings), \
         patch.object(es, "_stooq_quotes",
                      return_value={"NVDA.US": {"close": 175.0, "open": 170.0}}):
        lines = es.stocks_section()
    assert lines[0].startswith("NVIDIA (NVDA.US): 175")
    assert "+16.7% מהעלות" in lines[0]


def test_without_a_portfolio_the_watchlist_is_the_fallback():
    import portfolio
    with patch.object(portfolio, "load_holdings", return_value=[]), \
         patch.object(es, "fetch_stocks", return_value=["NVDA.US: 175.1"]) as watch:
        assert es.stocks_section() == ["NVDA.US: 175.1"]
    watch.assert_called_once()



def test_a_crypto_holding_is_priced_from_coingecko():
    import portfolio
    holdings = [{"kind": "crypto", "coingecko_id": "bitcoin", "symbol": "BTC",
                 "name": "ביטקוין (BTC)", "quantity": 0.35}]
    resp = MagicMock()
    resp.json.return_value = {"bitcoin": {"usd": 80000, "usd_24h_change": -1.2}}
    resp.raise_for_status.return_value = None
    with patch.object(portfolio, "load_holdings", return_value=holdings), \
         patch.object(es.requests, "get", return_value=resp):
        lines = es.stocks_section()
    assert "$80,000" in lines[0] and "-1.2% ב-24 השעות" in lines[0]


def test_a_coingecko_failure_is_reported_not_hidden():
    import portfolio
    holdings = [{"kind": "crypto", "coingecko_id": "bitcoin", "symbol": "BTC",
                 "name": "ביטקוין (BTC)", "quantity": 0.35}]
    with patch.object(portfolio, "load_holdings", return_value=holdings), \
         patch.object(es.requests, "get", side_effect=RuntimeError("down")):
        lines = es.stocks_section()
    assert "אין מחיר חי" in lines[0]


def test_a_mixed_portfolio_prices_each_side_from_its_own_feed():
    import portfolio
    holdings = [
        {"name": "NVIDIA", "symbol": "NVDA", "quantity": 10, "cost": 150.0},
        {"kind": "crypto", "coingecko_id": "bitcoin", "symbol": "BTC",
         "name": "ביטקוין (BTC)", "quantity": 0.35},
    ]
    with patch.object(portfolio, "load_holdings", return_value=holdings), \
         patch.object(es, "_stooq_quotes",
                      return_value={"NVDA.US": {"close": 175.0, "open": 170.0}}) as stooq, \
         patch.object(es, "_coingecko_quotes",
                      return_value={"bitcoin": {"price": 80000.0,
                                                "change_24h": 1.5}}) as gecko:
        lines = es.stocks_section()
    stooq.assert_called_once_with(["NVDA.US"])
    gecko.assert_called_once_with(["bitcoin"])
    assert lines[0].startswith("NVIDIA (NVDA.US): 175")
    assert lines[1].startswith("ביטקוין (BTC): $80,000")

