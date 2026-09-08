"""Tests for pricing Israeli securities from the exchange's own feeds.

What is being protected: a נייר number from the Excellence export prices as
an end-of-day row (agorot become shekels) or as a mutual fund's once-a-day
price, a number neither feed answers is reported as having no current price
with the export's last value beside it, and a feed being down never breaks
the review - it falls back, it never invents a number.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portfolio
import tase


EOD_PAYLOAD = {
    "Items": [
        {"TradeDate": "08/09/2026", "Change": 0.09, "BaseRate": 10910.0,
         "OpenRate": 10920.0, "CloseRate": 10920.0, "HighRate": 11020.0,
         "LowRate": 10680.0, "IfTraded": True},
    ],
    "TotalRec": 1,
}

FUND_PAYLOAD = [
    {"fundId": "05118393", "tradeDate": "2026-09-08T00:00:00",
     "purchasePrice": 128.61, "sellPrice": 128.61},
    {"fundId": "05118393", "tradeDate": "2026-09-07T00:00:00",
     "purchasePrice": 128.77, "sellPrice": 128.77},
]


def _json_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# --- identifying Israeli securities -------------------------------------------

def test_a_digits_only_symbol_is_a_tase_security_and_nothing_else_is():
    assert portfolio.tase_security_id({"symbol": "629014"}) == "629014"
    assert portfolio.tase_security_id({"symbol": "5118393"}) == "5118393"
    assert portfolio.tase_security_id({"symbol": "NVDA"}) is None
    assert portfolio.tase_security_id({"name": "x"}) is None
    assert portfolio.tase_security_id(
        {"kind": "crypto", "symbol": "BTC"}) is None


# --- exchange-traded securities ------------------------------------------------

def test_an_exchange_traded_security_prices_from_its_eod_row():
    with patch.object(tase.requests, "post",
                      return_value=_json_response(EOD_PAYLOAD)):
        quote = tase.security_quote("629014")
    assert quote == {"kind": "security", "close": 109.2,
                     "change_pct": 0.09, "date": "08/09"}


def test_agorot_become_shekels_but_the_change_is_used_as_given():
    with patch.object(tase.requests, "post",
                      return_value=_json_response(EOD_PAYLOAD)) as post:
        quote = tase.security_quote("629014")
    assert quote["close"] == 109.2  # 10920 agorot
    assert post.call_args.kwargs["json"]["oId"] == "629014"


def test_a_number_the_eod_feed_does_not_know_returns_none():
    with patch.object(tase.requests, "post",
                      return_value=_json_response({"Items": [], "TotalRec": 0})):
        assert tase.security_quote("5118393") is None


# --- mutual funds --------------------------------------------------------------

def test_a_mutual_fund_prices_from_its_daily_history():
    with patch.object(tase.requests, "post",
                      return_value=_json_response(FUND_PAYLOAD)):
        quote = tase.fund_quote("5118393")
    assert quote["kind"] == "fund"
    assert quote["close"] == 128.61  # already shekels, never divided
    assert quote["date"] == "08/09"
    assert quote["change_pct"] == (128.61 - 128.77) / 128.77 * 100


def test_a_number_the_fund_feed_does_not_know_returns_none():
    with patch.object(tase.requests, "post",
                      return_value=_json_response([])):
        assert tase.fund_quote("629014") is None


# --- the combined lookup --------------------------------------------------------

def test_a_fund_is_found_after_the_security_feed_draws_a_blank():
    calls = []

    def route(url, **kwargs):
        calls.append(url)
        if "historyeod" in url:
            return _json_response({"Items": [], "TotalRec": 0})
        return _json_response(FUND_PAYLOAD)

    with patch.object(tase.requests, "post", side_effect=route):
        out = tase.quotes(["5118393"])
    assert len(calls) == 2
    assert out["5118393"]["kind"] == "fund"
    assert out["5118393"]["close"] == 128.61


def test_a_dead_feed_means_no_quote_not_a_crash():
    with patch.object(tase.requests, "post", side_effect=RuntimeError("down")):
        assert tase.quotes(["629014"]) == {"629014": None}


# --- the daily review -----------------------------------------------------------

def test_the_review_prices_an_israeli_security_in_shekels_with_a_date():
    holdings = [{"name": "טבע", "symbol": "629014", "quantity": 100}]
    lines = portfolio.review_lines(
        holdings, {}, tase_quotes={"629014": {"kind": "security",
                                              "close": 109.2,
                                              "change_pct": 0.09,
                                              "date": "08/09"}})
    assert lines[0] == "טבע (629014): ₪109.20, +0.1% ביום המסחר, שווי ≈ ₪10,920, שער 08/09"


def test_the_review_counts_tase_risers_and_fallers():
    holdings = [
        {"name": "טבע", "symbol": "629014", "quantity": 100},
        {"name": "קרן", "symbol": "5118393", "quantity": 50},
    ]
    quotes = {"629014": {"kind": "security", "close": 109.2,
                         "change_pct": 0.09, "date": "08/09"},
              "5118393": {"kind": "fund", "close": 128.61,
                          "change_pct": -0.12, "date": "08/09"}}
    lines = portfolio.review_lines(holdings, {}, tase_quotes=quotes)
    assert "היום: 1 עלו, 1 ירדו" in lines[-1]


def test_an_unanswered_tase_number_falls_back_to_the_export_value():
    holdings = [{"name": "טבע", "symbol": "629014", "quantity": 100,
                 "value": 4100.0}]
    lines = portfolio.review_lines(
        holdings, {}, tase_quotes={"629014": None})
    assert "אין מחיר חי" in lines[0]
    assert 'שווי אחרון מהדו"ח 4100' in lines[0]


def test_a_tase_quote_never_invents_a_move_since_cost():
    # The export's cost for an Israeli security may be in agorot or shekels;
    # comparing it to a shekel price could be wrong a hundredfold, so the
    # line shows price, day move and value - and no cost comparison.
    holdings = [{"name": "טבע", "symbol": "629014", "quantity": 100,
                 "cost": 40.5}]
    lines = portfolio.review_lines(
        holdings, {}, tase_quotes={"629014": {"kind": "security",
                                              "close": 109.2,
                                              "change_pct": 0.09,
                                              "date": "08/09"}})
    assert "מהעלות" not in lines[0]
