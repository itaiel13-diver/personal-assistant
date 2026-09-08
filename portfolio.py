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
ordinary document.

Pricing is honest about its limits. stooq's free feed knows US and global
tickers, so a holding whose symbol looks like one (letters, short) is
priced as SYMBOL.US. An Israeli security number (נייר ערך מספר ...) has no
free feed the bot can reach - it is listed with its quantity and cost and
marked as having no live quote. A missing price is reported, never
invented, and percentages never mix currencies into a made-up total.
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
                             ("cost", _clean_number), ("price", _clean_number)):
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
    raw = (holding.get("symbol") or "").strip().upper()
    if not raw:
        return None
    if "." in raw:
        return raw
    if re.fullmatch(r"[A-Z]{1,6}", raw):
        return f"{raw}.US"
    return None


def review_lines(holdings: list, closes: dict) -> list:
    """The daily review as display lines: per holding, the day's move and the
    move since cost; then the count of risers and fallers.

    closes maps a stooq symbol to {"close": float|None, "open": float|None}.
    Pure and total in what it shows: every number comes from the export or
    from the feed it was handed, and a holding without a live quote says so.
    """
    lines = []
    up_today = down_today = 0
    gainers = losers = 0
    for h in holdings:
        name = h.get("name", "?")
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
    if not storage.save_portfolio(holdings, source=filename or ""):
        return ("❌ זיהיתי שזה דו\"ח תיק מאקסלנס, אבל אין לי מסד נתונים פעיל "
                "כדי לשמור אותו. תגיד לאיתי לבדוק את DATABASE_URL.")
    names = ", ".join(h["name"] for h in holdings[:6])
    more = f" ועוד {len(holdings) - 6}" if len(holdings) > 6 else ""
    return (
        f"✅ התיק נשמר - {len(holdings)} החזקות: {names}{more}.\n"
        "מכאן אני בודק אותו בעצמי כל ערב בסיכום: מושך מחירים עדכניים ומחשב "
        "מה עלה ומה ירד, בלי שתצטרך לשלוח שוב. ניירות ישראליים בלי סמל "
        "גלובלי יופיעו בלי מחיר חי - אין להם מקור חינמי שאני יכול להגיע אליו."
    )
