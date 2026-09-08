"""Itai's investment portfolio: handed over once, reviewed every evening.

He forwards the holdings export from the Excellence (אקסלנס) app on
WhatsApp - an xlsx - and from then on the daily review is the bot's job,
not his: the holdings are stored (storage.save_portfolio), and the evening
summary prices them every day without him resending anything.

The export's exact layout is whatever the app happens to produce, so the
parser is built around Hebrew header words, not fixed columns: it finds the
header row by looking for a name column and a quantity column, maps the
rest by synonyms, and reads rows until they run out. A file that does not
look like a holdings export returns None and the caller treats it as an
ordinary document. The export's own שווי אחזקה column is kept too: for a
security with no live feed it is the last value anyone actually stated, so
the review shows it - dated to the report, never as a live number.

Pricing is honest about its limits. stooq's free feed knows US and global
tickers, so a holding whose symbol looks like one (letters, short) is
priced as SYMBOL.US. An Israeli security number (נייר ערך מספר ...) has no
free feed the bot can reach - it is listed with its quantity and cost and
marked as having no live quote. A missing price is reported, never
invented, and percentages never mix currencies into a made-up total.

Bitcoin never appears in that export at all, so it arrives by chat instead:
"יש לי 0.35 ביטקוין" is parsed in code (never by the model - a misheard
amount would poison the daily review), stored as a holding with
kind="crypto", and priced once a day from CoinGecko's free JSON API - a
structured feed, so the number cannot be remembered either. Two things stay
unknown on purpose: the cost basis (until he gives one) and the currency mix
(coins are priced in USD, the export in shekels) - the review says "עלות לא
ידועה" and never folds dollars into the shekel lines. A fresh Excellence
export replaces only the export's own rows; crypto holdings survive it.
"""
import io
import logging
import re

import storage

logger = logging.getLogger(__name__)

EXPORT_EXTENSIONS = (".xlsx", ".xlsm", ".csv")

# Header synonyms, Hebrew first. A column is matched by the first group one
# of whose words appears in its header text; first match wins, so the
# narrower phrases (מחיר עלות) come before the bare ones (מחיר).
_NAME_WORDS = ("שם נייר", "נייר ערך", "שם הנייר", "שם")
_SYMBOL_WORDS = ("מספר נייר", "מס' נייר", "סמל", "סימבול", "מספר ני", "ticker", "symbol")
_QUANTITY_WORDS = ("כמות", "יחידות", "quantity")
_COST_WORDS = ("מחיר עלות", "שער עלות", "עלות ממוצעת", "עלות", "מחיר קנייה", "שער קנייה")
_PRICE_WORDS = ("שער אחרון", "מחיר נוכחי", "שער נוכחי", "שער", "מחיר")
_VALUE_WORDS = ("שווי אחזקה", "שווי החזקה", "שווי נייר", "שווי", "value")

MAX_HEADER_SCAN_ROWS = 20


def looks_like_export(filename: str) -> bool:
    """Cheap gate before parsing: only spreadsheet files can be an export."""
    return (filename or "").lower().strip().endswith(EXPORT_EXTENSIONS)


def _clean_number(value):
    """'1,234.5 ₪' -> 1234.5. None when it is not a number at all."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "").replace("₪", "").replace("$", "").replace("%", "")
    text = text.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _match_column(header: str):
    """Which portfolio field a header cell names, or None."""
    h = (header or "").strip()
    if not h:
        return None
    for field, words in (
        ("name", _NAME_WORDS),
        ("symbol", _SYMBOL_WORDS),
        ("quantity", _QUANTITY_WORDS),
        ("cost", _COST_WORDS),
        ("price", _PRICE_WORDS),
        ("value", _VALUE_WORDS),
    ):
        if any(w in h for w in words):
            return field
    return None


def _rows_from_file(filename: str, data: bytes):
    """Every sheet's rows as lists of cell values, whichever the container."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        import csv
        text = data.decode("utf-8-sig", errors="replace")
        yield from (list(row) for row in csv.reader(io.StringIO(text)))
        return
    import openpyxl
    workbook = openpyxl.load_workbook(
        io.BytesIO(data), read_only=True, data_only=True
    )
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                yield list(row)
    finally:
        workbook.close()


def parse_export(filename: str, data: bytes) -> list | None:
    """The holdings in an export file, or None when it is not one.

    A holdings export is recognised by its header row: a name column AND a
    quantity column among the first rows. Anything less is a spreadsheet
    that happens to mention stocks, and is left for the ordinary reader.
    """
    rows = _rows_from_file(filename, data)
    columns = None
    holdings = []
    scanned = 0
    for row in rows:
        scanned += 1
        if columns is None:
            if scanned > MAX_HEADER_SCAN_ROWS:
                return None
            mapping = {}
            for index, cell in enumerate(row):
                field = _match_column(str(cell) if cell is not None else "")
                if field and field not in mapping.values():
                    mapping[index] = field
            fields = set(mapping.values())
            if "name" in fields and "quantity" in fields:
                columns = mapping
            continue
        name_index = columns_by(columns, "name")
        name = row[name_index] if name_index is not None and name_index < len(row) else None
        name = (str(name).strip() if name is not None else "")
        if not name:
            if holdings:
                break  # the table ended; anything further is a totals block
            continue
        holding = {"name": name}
        for field, clean in (("symbol", str), ("quantity", _clean_number),
                             ("cost", _clean_number), ("price", _clean_number),
                             ("value", _clean_number)):
            index = columns_by(columns, field)
            if index is None or index >= len(row):
                continue
            value = row[index]
            if value is None:
                continue
            holding[field] = clean(value) if clean is not str else str(value).strip()
        holdings.append(holding)
    return holdings or None


def columns_by(mapping: dict, field: str):
    for index, f in mapping.items():
        if f == field:
            return index
    return None


def load_holdings() -> list:
    """The stored portfolio, [] when none was ever handed over."""
    return storage.load_portfolio()


def stooq_symbol(holding: dict) -> str | None:
    """The stooq symbol for a holding, when one can be derived.

    'NVDA' prices as NVDA.US; 'NVDA.US' stays as is. An Israeli security
    number is digits, and digits are not a ticker stooq knows - None, and
    the review says there is no live quote instead of guessing."""
    if holding.get("kind") == "crypto":
        return None
    raw = (holding.get("symbol") or "").strip().upper()
    if not raw:
        return None
    if "." in raw:
        return raw
    if re.fullmatch(r"[A-Z]{1,6}", raw):
        return f"{raw}.US"
    return None


def review_lines(holdings: list, closes: dict, crypto_quotes: dict | None = None) -> list:
    """The daily review as display lines: per holding, the day's move and the
    move since cost; then the count of risers and fallers.

    closes maps a stooq symbol to {"close": float|None, "open": float|None};
    crypto_quotes maps a CoinGecko id to {"price": float|None, "change_24h":
    float|None}, in USD. Pure and total in what it shows: every number comes
    from the export or from the feeds it was handed, a holding without a live
    quote says so, and a coin whose cost was never given says "עלות לא ידועה"
    instead of inventing a gain. Dollars stay inside the coin's own line -
    they are never folded into a shekel total.
    """
    crypto_quotes = crypto_quotes or {}
    lines = []
    up_today = down_today = 0
    gainers = losers = 0
    for h in holdings:
        name = h.get("name", "?")
        if h.get("kind") == "crypto":
            quote = crypto_quotes.get(h.get("coingecko_id")) or {}
            price = quote.get("price")
            change = quote.get("change_24h")
            quantity = h.get("quantity")
            cost = h.get("cost")
            if price is None:
                detail = f"{quantity:g} יח'" if quantity is not None else ""
                suffix = f" ({detail})" if detail else ""
                lines.append(f"{name}: אין מחיר חי{suffix}")
                continue
            parts = [f"${price:,.0f}"]
            if change is not None:
                parts.append(f"{change:+.1f}% ב-24 השעות")
                up_today += change > 0
                down_today += change < 0
            if quantity is not None:
                parts.append(f"שווי ≈ ${price * quantity:,.0f}")
            if cost and h.get("cost_currency") == "USD":
                since = (price - cost) / cost * 100
                parts.append(f"{since:+.1f}% מהעלות")
                gainers += since > 0
                losers += since < 0
            elif cost:
                parts.append(f"עלות {cost:g} (מטבע לא ידוע)")
            else:
                parts.append("עלות לא ידועה")
            lines.append(f"{name}: {', '.join(parts)}")
            continue
        symbol = stooq_symbol(h)
        quote = closes.get(symbol) if symbol else None
        close = (quote or {}).get("close")
        open_ = (quote or {}).get("open")
        cost = h.get("cost")
        quantity = h.get("quantity")
        if close is None:
            detail = []
            if quantity is not None:
                detail.append(f"{quantity:g} יח'")
            if cost is not None:
                detail.append(f"עלות {cost:g}")
            if h.get("value") is not None:
                # The export's own שווי אחזקה: the value as of the report's
                # day, never refreshed - said as such, not as a live number.
                detail.append(f'שווי אחרון מהדו"ח {h["value"]:g}')
            suffix = f" ({', '.join(detail)})" if detail else ""
            lines.append(f"{name}: אין מחיר חי{suffix}")
            continue
        day = (close - open_) / open_ * 100 if open_ else None
        parts = [f"{close:g}"]
        if day is not None:
            parts.append(f"{day:+.1f}% היום")
            up_today, down_today = up_today + (day > 0), down_today + (day < 0)
        if cost:
            since = (close - cost) / cost * 100
            parts.append(f"{since:+.1f}% מהעלות")
            gainers, losers = gainers + (since > 0), losers + (since < 0)
        lines.append(f"{name} ({symbol}): {', '.join(parts)}")
    priced = up_today + down_today
    if len(holdings) > 1:
        summary = []
        if priced:
            summary.append(f"היום: {up_today} עלו, {down_today} ירדו")
        if gainers + losers:
            summary.append(f"מהעלות: {gainers} ברווח, {losers} בהפסד")
        if summary:
            lines.append("סך התיק: " + " | ".join(summary))
    return lines


def import_export(filename: str, data: bytes) -> str | None:
    """Parse and store a forwarded export. The Hebrew confirmation to send
    back, or None when the file is not a holdings export at all."""
    try:
        holdings = parse_export(filename, data)
    except Exception as e:
        logger.error(f"Portfolio export parse failed for {filename!r}: {e}")
        return None
    if not holdings:
        return None
    kept = [h for h in load_holdings() if h.get("kind") == "crypto"]
    if not storage.save_portfolio(holdings + kept, source=filename or ""):
        return ("❌ זיהיתי שזה דו\"ח תיק מאקסלנס, אבל אין לי מסד נתונים פעיל "
                "כדי לשמור אותו. תגיד לאיתי לבדוק את DATABASE_URL.")
    names = ", ".join(h["name"] for h in holdings[:6])
    more = f" ועוד {len(holdings) - 6}" if len(holdings) > 6 else ""
    message = (
        f"✅ התיק נשמר - {len(holdings)} החזקות: {names}{more}.\n"
        "מכאן אני בודק אותו בעצמי כל ערב בסיכום: מושך מחירים עדכניים ומחשב "
        "מה עלה ומה ירד, בלי שתצטרך לשלוח שוב. ניירות ישראליים בלי סמל "
        "גלובלי יופיעו בלי מחיר חי - אין להם מקור חינמי שאני יכול להגיע אליו."
    )
    if kept:
        message += (f"\n{len(kept)} החזקות הקריפטו שנרשמו בצ'אט נשמרו "
                    "ולא נמחקו.")
    return message


# --- crypto by chat -----------------------------------------------------------
#
# The Excellence export has no coin rows, so a coin holding arrives as a chat
# message ("יש לי 0.35 ביטקוין") and is parsed here, in code. Only an explicit
# possession or update phrase counts: a price question or a passing mention of
# bitcoin must never write to the portfolio.

COINGECKO_IDS = {"bitcoin": "BTC"}
_COIN_NAMES_HE = {"bitcoin": "ביטקוין"}

# Longer aliases first, so "ביטקוינים" wins over its own prefix.
_COIN_ALIASES = {
    "ביטקוינים": "bitcoin",
    "ביטקוין": "bitcoin",
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "₿": "bitcoin",
}

_CRYPTO_INTENT = ("יש לי", "לי יש", "יש ברשותי", "מחזיק", "קניתי", "הוספתי",
                  "להוסיף", "תוסיף", "תרשום", "לרשום", "עדכן", "i have",
                  "i own", "add ")

_AMOUNT = r"([\d,]+(?:\.\d+)?)"


def parse_crypto_message(text: str) -> dict | None:
    """A coin holding stated in chat, or None.

    The amount is required to sit next to the coin's name, and the message to
    carry a possession/update phrase - both, not either."""
    t = " " + " ".join((text or "").lower().split()) + " "
    if not t.strip() or not any(w in t for w in _CRYPTO_INTENT):
        return None
    for alias, coin_id in _COIN_ALIASES.items():
        a = re.escape(alias)
        m = (re.search(_AMOUNT + r"\s*(?:יח'?\s*)?" + a, t)
             or re.search(a + r"\s*(?:של\s*)?" + _AMOUNT, t))
        if not m:
            continue
        parsed = {"coin_id": coin_id,
                  "quantity": float(m.group(1).replace(",", ""))}
        cost = re.search(r"(?:עלות|במחיר|קניתי\s+ב)\s*-?\s*" + _AMOUNT, t)
        if cost:
            parsed["cost"] = float(cost.group(1).replace(",", ""))
            if "$" in t or "דולר" in t:
                parsed["cost_currency"] = "USD"
            elif "₪" in t or "שקל" in t or 'ש"ח' in t:
                parsed["cost_currency"] = "ILS"
        return parsed
    return None


def upsert_crypto(coin_id: str, quantity: float, cost=None,
                  cost_currency=None) -> str:
    """Store or replace one coin holding, keeping everything else in the
    portfolio - including the Excellence export's rows - untouched."""
    holdings = [h for h in load_holdings() if h.get("coingecko_id") != coin_id]
    symbol = COINGECKO_IDS.get(coin_id, coin_id.upper())
    name_he = _COIN_NAMES_HE.get(coin_id, coin_id)
    holding = {"kind": "crypto", "coingecko_id": coin_id, "symbol": symbol,
               "name": f"{name_he} ({symbol})", "quantity": quantity}
    if cost is not None:
        holding["cost"] = cost
        if cost_currency:
            holding["cost_currency"] = cost_currency
    holdings.append(holding)
    if not storage.save_portfolio(holdings, source="whatsapp-chat"):
        return ("❌ זיהיתי החזקת קריפטו, אבל אין לי מסד נתונים פעיל כדי "
                "לשמור אותה. תגיד לאיתי לבדוק את DATABASE_URL.")
    lines = [f"✅ נרשם בתיק: {quantity:g} {symbol} ({name_he}).",
             "כל ערב בסיכום אמשוך את המחיר העדכני מ-CoinGecko בדולרים ואציג "
             "שווי ותנועה יומית."]
    if cost is not None and cost_currency == "USD":
        lines.append(f"רשמתי גם עלות של ${cost:g} - אחשב גם רווח/הפסד מהעלות.")
    elif cost is not None:
        lines.append(f"רשמתי עלות {cost:g}, אבל המטבע לא ברור ולא ניתן "
                     "להשוות למחיר הדולרי - אציג אותה בלי חישוב רווח/הפסד.")
    else:
        lines.append("מחיר העלות לא ידוע לי, אז אכתוב 'עלות לא ידועה' ולא "
                     "אמציא רווח או הפסד. אם תשלח גם את מחיר הקנייה בדולרים - "
                     "אחשב.")
    return "\n".join(lines)


def handle_crypto_message(text: str) -> str | None:
    """Store a coin holding stated in chat and confirm it in Hebrew, or None
    when the message is not one."""
    parsed = parse_crypto_message(text)
    if not parsed:
        return None
    try:
        return upsert_crypto(**parsed)
    except Exception as e:
        logger.error(f"Crypto holding save failed: {e}")
        return "❌ משהו השתבש בשמירת ההחזקה. נסה שוב בעוד רגע."

