"""Tests for the portfolio Itai hands over once and the bot reviews daily.

What is being protected: the parser recognises an Excellence holdings export
by its Hebrew headers without breaking on an ordinary spreadsheet, a holding
with no reachable live price is reported as such and never given an invented
number, and storing replaces the old export whole so a fresh one is the
truth from that evening on.
"""
import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portfolio


def _xlsx(rows):
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


EXCELLENCE_LIKE = [
    ['דו"ח תיק השקעות', None, None, None, None],
    ['שם נייר', 'מספר נייר', 'כמות', 'מחיר עלות', 'שער אחרון'],
    ['NVIDIA', 'NVDA', 10, 150.0, 175.1],
    ['טבע', '693014', 100, 40.5, 41.0],
]


def test_an_excellence_export_is_parsed_by_its_hebrew_headers():
    holdings = portfolio.parse_export("תיק.xlsx", _xlsx(EXCELLENCE_LIKE))
    assert len(holdings) == 2
    assert holdings[0] == {"name": "NVIDIA", "symbol": "NVDA",
                           "quantity": 10.0, "cost": 150.0, "price": 175.1}
    assert holdings[1]["name"] == "טבע" and holdings[1]["quantity"] == 100.0


def test_an_ordinary_spreadsheet_is_not_a_portfolio():
    data = _xlsx([["תאריך", "סניף", "הערות"], ["08/09", "יבנה", "VOC"]])
    assert portfolio.parse_export("voc.xlsx", data) is None


def test_only_spreadsheets_are_even_parsed():
    assert portfolio.looks_like_export("תיק.xlsx")
    assert portfolio.looks_like_export("export.csv")
    assert not portfolio.looks_like_export("מסמך.pdf")


def test_numbers_survive_commas_shekel_signs_and_parentheses():
    assert portfolio._clean_number("1,234.5 ₪") == 1234.5
    assert portfolio._clean_number("(12.0)") == -12.0
    assert portfolio._clean_number("אין") is None


def test_a_us_ticker_maps_to_stooq_and_an_israeli_number_does_not():
    assert portfolio.stooq_symbol({"symbol": "NVDA"}) == "NVDA.US"
    assert portfolio.stooq_symbol({"symbol": "NVDA.US"}) == "NVDA.US"
    assert portfolio.stooq_symbol({"symbol": "693014"}) is None
    assert portfolio.stooq_symbol({"name": "x"}) is None


def test_the_review_shows_the_days_move_and_the_move_since_cost():
    holdings = [
        {"name": "NVIDIA", "symbol": "NVDA", "quantity": 10, "cost": 150.0},
        {"name": "טבע", "symbol": "693014", "quantity": 100, "cost": 40.5},
    ]
    lines = portfolio.review_lines(holdings, {"NVDA.US": {"close": 175.0, "open": 170.0}})
    assert lines[0] == "NVIDIA (NVDA.US): 175, +2.9% היום, +16.7% מהעלות"
    assert "טבע" in lines[1] and "אין מחיר חי" in lines[1]
    assert "1 עלו" in lines[2] and "1 ברווח" in lines[2]


def test_a_symbol_the_feed_does_not_know_is_reported_not_dropped():
    lines = portfolio.review_lines(
        [{"name": "דמיונית", "symbol": "FAKE", "quantity": 1, "cost": 1.0}],
        {"FAKE.US": {"close": None, "open": None}},
    )
    assert "אין מחיר חי" in lines[0]


def test_the_exports_value_column_is_kept_and_shown_for_unpriced_holdings():
    rows = [
        ['שם נייר', 'מספר נייר', 'כמות', 'מחיר עלות', 'שער אחרון', 'שווי אחזקה'],
        ['טבע', '693014', 100, 40.5, 41.0, 4100.0],
    ]
    holdings = portfolio.parse_export("תיק.xlsx", _xlsx(rows))
    assert holdings[0]["value"] == 4100.0
    lines = portfolio.review_lines(holdings, {})
    assert "אין מחיר חי" in lines[0]
    assert 'שווי אחרון מהדו"ח 4100' in lines[0]


def test_import_stores_the_export_and_confirms_in_hebrew():
    saved = {}
    with patch.object(portfolio.storage, "save_portfolio",
                      side_effect=lambda h, source="": saved.update(holdings=h) or True):
        out = portfolio.import_export("תיק.xlsx", _xlsx(EXCELLENCE_LIKE))
    assert out.startswith("✅") and "2 החזקות" in out
    assert saved["holdings"][0]["name"] == "NVIDIA"


def test_import_of_a_non_export_returns_none_and_stores_nothing():
    with patch.object(portfolio.storage, "save_portfolio") as save:
        assert portfolio.import_export("voc.xlsx", _xlsx([["א", "ב"]])) is None
    save.assert_not_called()


def test_without_a_database_the_import_says_so():
    with patch.object(portfolio.storage, "save_portfolio", return_value=False):
        out = portfolio.import_export("תיק.xlsx", _xlsx(EXCELLENCE_LIKE))
    assert out.startswith("❌") and "DATABASE_URL" in out



# --- crypto by chat -----------------------------------------------------------

BTC_HOLDING = {"kind": "crypto", "coingecko_id": "bitcoin", "symbol": "BTC",
               "name": "ביטקוין (BTC)", "quantity": 0.35}


def test_a_bitcoin_holding_is_parsed_from_chat():
    assert portfolio.parse_crypto_message("ויש לי גם 0.35 btc") == \
        {"coin_id": "bitcoin", "quantity": 0.35}
    assert portfolio.parse_crypto_message("יש לי 0.35 ביטקוין")["quantity"] == 0.35
    assert portfolio.parse_crypto_message("אני מחזיק 2 ביטקוין")["quantity"] == 2.0
    assert portfolio.parse_crypto_message("add 0.5 bitcoin")["quantity"] == 0.5


def test_a_mention_without_possession_is_not_a_holding():
    assert portfolio.parse_crypto_message("מה המחיר של ביטקוין היום?") is None
    assert portfolio.parse_crypto_message("0.35 btc") is None
    assert portfolio.parse_crypto_message("שמעת על הביטקוין?") is None


def test_cost_and_currency_are_captured_only_when_stated():
    p = portfolio.parse_crypto_message("יש לי 0.35 ביטקוין שקניתי ב-60,000 דולר")
    assert p["cost"] == 60000.0 and p["cost_currency"] == "USD"
    p = portfolio.parse_crypto_message("יש לי 0.35 ביטקוין בעלות 60000")
    assert p["cost"] == 60000.0 and "cost_currency" not in p


def test_crypto_upsert_keeps_the_export_rows():
    stored = [{"name": "NVIDIA", "symbol": "NVDA", "quantity": 10.0}]
    saved = {}
    with patch.object(portfolio, "load_holdings", return_value=stored), \
         patch.object(portfolio.storage, "save_portfolio",
                      side_effect=lambda h, source="": saved.update(holdings=h) or True):
        out = portfolio.handle_crypto_message("יש לי 0.35 ביטקוין")
    assert out.startswith("✅") and "עלות לא ידועה" in out
    assert saved["holdings"][0]["name"] == "NVIDIA"
    crypto = saved["holdings"][1]
    assert crypto["kind"] == "crypto" and crypto["quantity"] == 0.35
    assert crypto["coingecko_id"] == "bitcoin"


def test_a_second_bitcoin_message_replaces_the_first():
    saved = {}
    with patch.object(portfolio, "load_holdings", return_value=[dict(BTC_HOLDING)]), \
         patch.object(portfolio.storage, "save_portfolio",
                      side_effect=lambda h, source="": saved.update(holdings=h) or True):
        portfolio.handle_crypto_message("יש לי 0.5 ביטקוין")
    assert len(saved["holdings"]) == 1
    assert saved["holdings"][0]["quantity"] == 0.5


def test_a_fresh_excel_import_preserves_crypto():
    saved = {}
    with patch.object(portfolio, "load_holdings", return_value=[dict(BTC_HOLDING)]), \
         patch.object(portfolio.storage, "save_portfolio",
                      side_effect=lambda h, source="": saved.update(holdings=h) or True):
        out = portfolio.import_export("תיק.xlsx", _xlsx(EXCELLENCE_LIKE))
    kinds = [h.get("kind") for h in saved["holdings"]]
    assert kinds == [None, None, "crypto"]
    assert "קריפטו" in out


def test_crypto_is_never_mapped_to_a_stooq_symbol():
    assert portfolio.stooq_symbol({"kind": "crypto", "symbol": "BTC"}) is None


def test_the_review_prices_a_coin_from_the_feed_it_was_handed():
    lines = portfolio.review_lines(
        [dict(BTC_HOLDING)], {},
        {"bitcoin": {"price": 80000.0, "change_24h": 1.5}})
    assert lines[0] == ("ביטקוין (BTC): $80,000, +1.5% ב-24 השעות, "
                        "שווי ≈ $28,000, עלות לא ידועה")


def test_a_coin_without_a_quote_says_so_and_a_usd_cost_is_compared():
    lines = portfolio.review_lines([dict(BTC_HOLDING)], {}, {})
    assert "אין מחיר חי" in lines[0] and "0.35 יח'" in lines[0]
    with_cost = dict(BTC_HOLDING, cost=40000.0, cost_currency="USD")
    lines = portfolio.review_lines(
        [with_cost], {}, {"bitcoin": {"price": 80000.0, "change_24h": -2.0}})
    assert "+100.0% מהעלות" in lines[0]
    unknown_ccy = dict(BTC_HOLDING, cost=40000.0)
    lines = portfolio.review_lines(
        [unknown_ccy], {}, {"bitcoin": {"price": 80000.0, "change_24h": None}})
    assert "עלות 40000 (מטבע לא ידוע)" in lines[0]
    assert "מהעלות" not in lines[0]

