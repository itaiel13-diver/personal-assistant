"""The 20:00 message: what today was, and what the day left open.

Every other routine in this project reacts to one fact - a reminder came due,
an email arrived, a file was shared. This one closes the day. Itai asked for
it in his own words (2026-09-08): every evening around 20:00, one WhatsApp
message with (1) a summary of what he did today, (2) follow-up questions
where the day is missing information - a VOC he held and never wrote down,
anything the assistant should know - (3) the top 3 football results, Israel
and the world, and (4) his stocks.

It is the only routine allowed to think, and it thinks once a day: one
llm.ask call per evening, claimed in the proactive log BEFORE the call so two
overlapping ticks cannot spend it twice. When every provider is out of quota
the message still goes out, rule-based: the scores and prices are real either
way, and the standing questions do not need a model to be asked.

Two deliberate data-source choices:

- Football comes from TheSportsDB's free JSON API (key "3" is its published
  public tier), not from a search: a structured feed cannot hallucinate a
  score, and it costs no Tavily credit.
- Stocks come from STOCK_SYMBOLS, stooq symbols such as NVDA.US, quoted from
  stooq's free CSV endpoint. His broker (Excellence) offers no public API to
  retail clients - verified 2026-09-08 - so until he is given one, the
  watchlist is the source of truth. An empty watchlist is not hidden: the
  summary asks him for his symbols instead of inventing numbers.
"""
import csv
import io
import logging
import os
from datetime import datetime, time

import requests

import llm
import storage

logger = logging.getLogger(__name__)

SUMMARY_AT = time(20, 0)

FOOTBALL_URL = "https://www.thesportsdb.com/api/v1/json/3/eventsday.php"
STOOQ_URL = "https://stooq.com/q/l/"

HTTP_TIMEOUT = 15

# Israel first, then the leagues he actually follows. A league containing one
# of these words ranks before anything else; the rest of the world follows.
LEAGUE_PRIORITY = (
    "israel", "champions league", "premier league", "la liga", "serie a",
    "bundesliga", "ligue 1", "europa league", "world cup", "euro",
)

MAX_FOOTBALL_LINES = 6
MAX_HISTORY_CHARS = 6000


def stock_symbols() -> list:
    """The watchlist, from configuration rather than code: Itai's holdings
    change without a deploy."""
    raw = os.environ.get("STOCK_SYMBOLS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def fetch_football(day: str) -> list:
    """Today's finished football results as display lines, Israel first.

    Returns [] on any failure - the summary then says the section is missing
    rather than skipping the whole message over a sports API hiccup."""
    try:
        resp = requests.get(
            FOOTBALL_URL, params={"d": day, "s": "Soccer"}, timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        events = (resp.json() or {}).get("events") or []
    except Exception as e:
        logger.error(f"Football fetch failed for {day}: {e}")
        return []

    finished = []
    for ev in events:
        home, away = ev.get("intHomeScore"), ev.get("intAwayScore")
        if home is None or away is None:
            continue
        league = (ev.get("strLeague") or "").strip()
        line = f"{ev.get('strHomeTeam')} {home}-{away} {ev.get('strAwayTeam')} ({league})"
        finished.append((league.lower(), line))

    def rank(item):
        league, _ = item
        for i, key in enumerate(LEAGUE_PRIORITY):
            if key in league:
                return i
        return len(LEAGUE_PRIORITY)

    finished.sort(key=rank)
    return [line for _, line in finished[:MAX_FOOTBALL_LINES]]


def fetch_stocks(symbols: list) -> list:
    """One CSV call for the whole watchlist: last close and the day's move
    from open. A symbol stooq does not know is reported, not dropped - a
    silently missing holding reads as a worse day than it was."""
    if not symbols:
        return []
    try:
        resp = requests.get(
            STOOQ_URL,
            params={"s": ",".join(s.lower() for s in symbols),
                    "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
    except Exception as e:
        logger.error(f"Stock fetch failed for {symbols}: {e}")
        return []

    lines = []
    for row in rows:
        symbol = (row.get("Symbol") or "").upper()
        close, open_ = row.get("Close") or "", row.get("Open") or ""
        if close in ("", "N/D"):
            lines.append(f"{symbol}: אין נתון זמין")
            continue
        try:
            change = (float(close) - float(open_)) / float(open_) * 100
            lines.append(f"{symbol}: {close} ({change:+.1f}% מהפתיחה)")
        except (ValueError, ZeroDivisionError):
            lines.append(f"{symbol}: {close}")
    return lines


def _todays_conversation(owner_phone: str) -> str:
    """The raw material for 'what he did today': his recent conversation with
    the assistant, rendered as plain lines. History entries carry no
    timestamps, so this is the running window (the last ~40 turns), which on
    any ordinary day is dominated by today."""
    if not (storage.enabled() and owner_phone):
        return ""
    try:
        history = storage.load_history(owner_phone)
    except Exception as e:
        logger.error(f"Could not load history for the evening summary: {e}")
        return ""
    lines = []
    for entry in history:
        role = entry.get("role", "")
        if role not in ("user", "model"):
            continue
        texts = [p.get("text", "") for p in entry.get("parts", [])
                 if isinstance(p, dict) and p.get("text")]
        if texts:
            speaker = "איתי" if role == "user" else "העוזר"
            lines.append(f"{speaker}: {' '.join(texts)}")
    rendered = "\n".join(lines)
    return rendered[-MAX_HISTORY_CHARS:]


_SYSTEM = (
    "אתה העוזר האישי של איתי אלפסי, מנהל אזור שפלה בסמסונג ישראל. "
    "אתה כותב לו עכשיו את סיכום הערב היומי בוואטסאפ - בעברית, קצר, ישיר, "
    "בלי הקדמות ובלי סיכומים מיותרים."
)

_PROMPT = """היום {date}. הרכב את הודעת סיכום הערב של איתי, בדיוק במבנה הזה:

🌙 *סיכום יום - {date_he}*

📋 *מה עשית היום*
2-4 נקודות קצרות, מבוססות רק על השיחה שלו איתך היום (מופיעה למטה). אם היום
כמעט ריק - שורה אחת שאומרת את זה, בלי להמציא פעילות.

⚽ *כדורגל*
בחר את 3 התוצאות המעניינות מרשימת התוצאות למטה - ישראל קודם. אם הרשימה ריקה,
כתוב שאין תוצאות זמינות כרגע.

📈 *מניות*
הצג את רשימת המניות למטה כמו שהיא, מילת פרשנות אחת לכל היותר. אם הרשימה ריקה,
כתוב שאין מניות במעקב ושאפשר לשלוח סמלים או להעביר דו"ח אקסלנס באקסל.

❓ *לסגור את היום*
1-2 שאלות המשך ספציפיות למה שהיום השאיר פתוח - למשל VOC שנעשה ולא נכתב, או
משהו שצריך לדעת על מחר. אם היום הריק לגמרי, שאל את שתי השאלות הקבועות.

כללים: אל תמציא עובדות שלא בשיחה או בנתונים. אל תוסיף קישורים. עד 20 שורות.

--- שיחת היום ---
{history}

--- תוצאות כדורגל ---
{football}

--- מניות ---
{stocks}
"""

_FALLBACK_QUESTIONS = (
    "- עשית היום VOC שלא כתבת? ספר לי וארשום אותו\n"
    "- יש משהו שכדאי שאדע על היום או על מחר?"
)

_HEBREW_DAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")


def build_message(now: datetime, owner_phone: str) -> str:
    """The whole evening message. Never raises: a routine that fails takes the
    tick down with it, and an evening without the summary is better than an
    evening without the reminders that share the tick."""
    date_he = f"יום {_HEBREW_DAYS[now.weekday()]} {now:%d/%m}"
    football = fetch_football(f"{now:%Y-%m-%d}")
    stocks = fetch_stocks(stock_symbols())
    history = _todays_conversation(owner_phone)

    prompt = _PROMPT.format(
        date=f"{now:%Y-%m-%d}",
        date_he=date_he,
        history=history or "(אין שיחה מתועדת מהיום)",
        football="\n".join(football) or "(אין תוצאות זמינות)",
        stocks="\n".join(stocks) or "(ריק)",
    )
    try:
        text = llm.ask(prompt, system=_SYSTEM, max_tokens=800, temperature=0.3)
    except Exception as e:
        # llm.ask already returns None when every provider is spent; this is
        # for the failure its docstring does not promise away.
        logger.error(f"Evening summary model call failed: {e}")
        text = None
    if text:
        return text
    return _fallback_message(date_he, football, stocks)


def _fallback_message(date_he: str, football: list, stocks: list) -> str:
    """No model quota, still a real summary: the data sections are facts, and
    the questions are the standing ones Itai named himself."""
    sections = [f"🌙 *סיכום יום - {date_he}*"]
    sections.append("⚽ *כדורגל*\n" + ("\n".join(football[:3]) if football
                                       else "אין תוצאות זמינות כרגע."))
    if stocks:
        sections.append("📈 *מניות*\n" + "\n".join(stocks))
    else:
        sections.append(
            "📈 *מניות*\nלא הוגדרו מניות למעקב. שלח לי את הסמלים (למשל NVDA.US) "
            "או העבר את דו\"ח התיק מאקסלנס כקובץ אקסל, ואעקוב."
        )
    sections.append("❓ *לסגור את היום*\n" + _FALLBACK_QUESTIONS)
    return "\n\n".join(sections)
