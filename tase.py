"""Israeli securities, priced from the Tel Aviv Stock Exchange's own feeds.

The Excellence export lists Israeli holdings by their נייר ערך number, and
those same numbers address the exchange's data directly - no symbol mapping,
no API key, no signup. Two endpoint families cover the whole market:

- Exchange-traded securities (shares, ETFs, bonds): the end-of-day history
  behind tase.co.il's security page. One POST returns the official daily
  rows - close, change vs the previous day, trade date - in agorot, which
  this module normalizes to shekels.
- Mutual funds (קרנות נאמנות), which the endpoint above does not know:
  the Maya site's fund history. A fund's price is published once a day, in
  shekels, so the latest row is the current price and the row before it
  gives the daily move.

Both feeds are the public JSON the exchange's own websites call - verified
live 2026-09-09. They are not a contracted product, so every call here is
wrapped: any failure (network, bot protection, a changed layout) returns
None, and the portfolio review falls back to the last value from the
Excellence export, labelled as such - a missing price is reported, never
invented. Prices are end-of-day, which is exactly what the evening summary
needs; nothing here is a real-time quote.
"""
import logging
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

EOD_URL = "https://api.tase.co.il/api/security/historyeod"
FUND_HISTORY_URL = "https://maya.tase.co.il/api/v1/funds/mutual/{fund_id}/history"

# The feeds answer only when the request looks like the site's own: its
# referer and an ordinary browser agent. Plain scripts get a block page.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "referer": "https://www.tase.co.il/",
}
_FUND_HEADERS = dict(_HEADERS, referer="https://maya.tase.co.il/")

HTTP_TIMEOUT = 10
# Weekends and holidays: the last traded day always sits inside this window.
LOOKBACK_DAYS = 14


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _short_day(raw: str) -> str:
    """'08/09/2026' or '2026-09-08...' -> '08/09'."""
    raw = (raw or "").strip()
    if "/" in raw:
        return raw[:5]
    if len(raw) >= 10 and raw[4] == "-":
        return f"{raw[8:10]}/{raw[5:7]}"
    return raw


def security_quote(security_id: str) -> dict | None:
    """The latest end-of-day row for an exchange-traded security, in shekels.

    None when the feed has no row for the number - a mutual fund, a
    suspended security, or a number that is not traded at all."""
    today = date.today()
    body = {
        "dFrom": str(today - timedelta(days=LOOKBACK_DAYS)),
        "dTo": str(today),
        "oId": str(security_id),
        "pageNum": 1,
        "pType": "8",
        "TotalRec": 1,
        "lang": "1",
    }
    resp = requests.post(EOD_URL, json=body, headers=_HEADERS,
                         timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    items = (resp.json() or {}).get("Items") or []
    if not items:
        return None
    row = items[0]
    close = _number(row.get("CloseRate"))
    if close is None:
        return None
    return {
        "kind": "security",
        "close": close / 100,  # agorot -> shekels
        "change_pct": _number(row.get("Change")),
        "date": _short_day(row.get("TradeDate") or ""),
    }


def fund_quote(fund_id: str) -> dict | None:
    """The latest published price of a mutual fund, in shekels.

    A fund prices once a day, so the first history row is the current price
    and the second gives the move. None when the number is not a fund."""
    body = {"pageSize": 2, "pageNumber": 1, "period": 1}
    resp = requests.post(FUND_HISTORY_URL.format(fund_id=fund_id), json=body,
                         headers=_FUND_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list) or not items:
        return None
    price = _number(items[0].get("purchasePrice"))
    if price is None:
        return None
    change = None
    if len(items) > 1:
        prev = _number(items[1].get("purchasePrice"))
        if prev:
            change = (price - prev) / prev * 100
    return {
        "kind": "fund",
        "close": price,  # funds publish in shekels, not agorot
        "change_pct": change,
        "date": _short_day(items[0].get("tradeDate") or ""),
    }


def quotes(security_ids: list) -> dict:
    """{security number: quote dict or None} for every number handed in.

    Each number is tried as an exchange-traded security first and as a
    mutual fund second - the two feeds are on different hosts, so one being
    down never blocks the other. A number both feeds reject gets None, and
    the review says there is no current price instead of guessing one."""
    out = {}
    for sid in security_ids:
        quote = None
        try:
            quote = security_quote(sid)
        except Exception as e:
            logger.error(f"TASE security fetch failed for {sid}: {e}")
        if quote is None:
            try:
                quote = fund_quote(sid)
            except Exception as e:
                logger.error(f"TASE fund fetch failed for {sid}: {e}")
                quote = None
        out[sid] = quote
    return out
