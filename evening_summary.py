"""The 20:00 message: what today was, and what the day left open.

Every other routine in this project reacts to one fact - a reminder came due,
an email arrived, a file was shared. This one closes the day. Itai asked for
it in his own words (2026-09-08): every evening around 20:00, one WhatsApp
message with (1) a summary of what he did today, (2) follow-up questions
where the day is missing information - a VOC he held and never wrote down,
anything the assistant should know - (3) football: the day's results and the
coming days' fixtures, Israel and the world, and (4) his portfolio.

It is the only routine allowed to think, and it thinks once a day: one
llm.ask call per evening, claimed in the proactive log BEFORE the call so two
overlapping ticks cannot spend it twice.

Where the model may and may not write. The first summary (2026-09-08)
announced a World Cup final that was not happening, because the model was
handed the football data and allowed to phrase the section - and it reached
for its own memory instead. That is why the message is now split:

- The DATA sections - football results, upcoming fixtures, stocks - are
  composed in code, character for character, from the structured feeds.
  The model never sees them and cannot add to them. An empty feed becomes
  "no games", not an invitation to remember one.
- The model writes only the two sections that need judgement - the recap
  of his day and the follow-up questions - and its prompt grounds both in
  the day's stored conversation alone. When every provider is out of quota
  the message still goes out with a plain recap note and the standing
  questions.

Three deliberate data-source choices:

- Football comes from TheSportsDB's free JSON API (key "3" is its published
  public tier), not from a search: a structured feed cannot hallucinate a
  score, and it costs no Tavily credit.
- The portfolio comes from the holdings export Itai forwarded once from the
  Excellence app (portfolio.py). His broker offers no public API to retail
  clients - verified 2026-09-08 - so the stored export plus stooq's free
  prices is the source of truth.
- STOCK_SYMBOLS (stooq symbols such as NVDA.US) remains the fallback for
  when no export was ever given. An empty everything is not hidden: the
  summary asks him for his holdings instead of inventing numbers.
"""
import csv
import io
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import llm
import portfolio
import storage

logger = logging.getLogger(__name__)

SUMMARY_AT = time(20, 0)

FOOTBALL_URL = "https://www.thesportsdb.com/api/v1/json/3/eventsday.php"
STOOQ_URL = "https://stooq.com/q/l/"

HTTP_TIMEOUT = 15

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Israel first, then the leagues he actually follows. A league containing one
# of these words ranks before anything else; the rest of the world follows.
LEAGUE_PRIORITY = (
    "israel", "champions league", "premier league", "la liga", "serie a",
    "bundesliga", "ligue 1", "europa league", "world cup", "euro",
)

MAX_FOOTBALL_LINES = 6
MAX_FIXTURE_LINES = 5
FIXTURE_DAYS_AHEAD = 3
MAX_HISTORY_CHARS = 6000


def stock_symbols() -> list:
    """The fallback watchlist, from configuration rather than code. Used only
    when Itai never forwarded a portfolio export."""
    raw = os.environ.get("STOCK_SYMBOLS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _league_rank(league: str) -> int:
    league = league.lower()
    for i, key in enumerate(LEAGUE_PRIORITY):
        if key in league:
            return i
    return len(LEAGUE_PRIORITY)


def _events_for_day(day: str) -> list:
    """One day's football events from the feed, [] on any failure - the
    summary then says the section is missing rather than skipping the whole
    message over a sports API hiccup."""
    try:
        resp = requests.get(
            FOOTBALL_URL, params={"d": day, "s": "Soccer"}, timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("events") or []
    except Exception as e:
        logger.error(f"Football fetch failed for {day}: {e}")
        return []


def fetch_football(day: str) -> list:
    """Today's finished football results as display lines, Israel first."""
    finished = []
    for ev in _events_for_day(day):
        home, away = ev.get("intHomeScore"), ev.get("intAwayScore")
        if home is None or away is None:
            continue
        league = (ev.get("strLeague") or "").strip()
        line = f"{ev.get('strHomeTeam')} {home}-{away} {ev.get('strAwayTeam')} ({league})"
        finished.append((league, line))

    finished.sort(key=lambda item: _league_rank(item[0]))
    return [line for _, line in finished[:MAX_FOOTBALL_LINES]]


def _fixture_when(ev: dict, day: str) -> datetime | None:
    """Kickoff as an Israel-time datetime. The feed's strTimestamp is UTC
    epoch; dateEvent+strTime is its fallback, also UTC."""
    stamp = (ev.get("strTimestamp") or "").strip()
    try:
        if stamp:
            try:
                parsed = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
            except ValueError:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ISRAEL_TZ)
        raw_time = (ev.get("strTime") or "00:00:00").strip()
        return datetime.fromisoformat(f"{day}T{raw_time}").replace(
            tzinfo=timezone.utc).astimezone(ISRAEL_TZ)
    except (ValueError, TypeError, OSError):
        return None


def fetch_fixtures(day: str, days_ahead: int = FIXTURE_DAYS_AHEAD) -> list:
    """The coming days' fixtures as display lines, Israel first.

    Only events the feed actually returned, with no score yet - a fixture
    exists here because TheSportsDB listed it, never because anyone
    remembered it."""
    base = datetime.strptime(day, "%Y-%m-%d")
    fixtures = []
    for offset in range(1, days_ahead + 1):
        d = f"{base + timedelta(days=offset):%Y-%m-%d}"
        for ev in _events_for_day(d):
            if ev.get("intHomeScore") is not None or ev.get("intAwayScore") is not None:
                continue
            league = (ev.get("strLeague") or "").strip()
            home, away = ev.get("strHomeTeam"), ev.get("strAwayTeam")
            if not home or not away:
                continue
            when = _fixture_when(ev, d)
            if when:
                label = f"יום {_HEBREW_DAYS[when.weekday()]} {when:%d/%m %H:%M}"
            else:
                label = f"{d[8:10]}/{d[5:7]}"
            fixtures.append((_league_rank(league), f"{label}: {home} נגד {away} ({league})"))

    fixtures.sort(key=lambda item: item[0])
    return [line for _, line in fixtures[:MAX_FIXTURE_LINES]]


def _stooq_quotes(symbols: list) -> dict:
    """One CSV call for the whole list: {SYMBOL: {"close": float|None,
    "open": float|None}}. A symbol stooq does not know stays in the map with
    None prices - a silently missing holding reads as a worse day than it
    was."""
    if not symbols:
        return {}
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
        return {}

    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    quotes = {}
    for row in rows:
        symbol = (row.get("Symbol") or "").upper()
        quotes[symbol] = {"close": number(row.get("Close")),
                          "open": number(row.get("Open"))}
    return quotes


def fetch_stocks(symbols: list) -> list:
    """The fallback watchlist as display lines: last close and the day's
    move from open."""
    lines = []
    quotes = _stooq_quotes(symbols)
    for symbol in symbols:
        quote = quotes.get(symbol.upper())
        close, open_ = (quote or {}).get("close"), (quote or {}).get("open")
        if close is None:
            lines.append(f"{symbol.upper()}: אין נתון זמין")
            continue
        if open_:
            change = (close - open_) / open_ * 100
            lines.append(f"{symbol.upper()}: {close:g} ({change:+.1f}% מהפתיחה)")
        else:
            lines.append(f"{symbol.upper()}: {close:g}")
    return lines


def stocks_section() -> list:
    """The portfolio review when Itai handed one over, else the configured
    watchlist. Every number in either comes from the export or the feed."""
    holdings = portfolio.load_holdings()
    if holdings:
        symbols = [s for s in (portfolio.stooq_symbol(h) for h in holdings) if s]
        return portfolio.review_lines(holdings, _stooq_quotes(symbols))
    return fetch_stocks(stock_symbols())


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
    "אתה כותב לו עכשיו שני חלקים מתוך סיכום הערב היומי בוואטסאפ - בעברית, "
    "קצר, ישיר, בלי הקדמות."
)

_PROMPT = """היום {date}. כתוב רק את שני החלקים הבאים של סיכום הערב של איתי,
בדיוק במבנה הזה ובלי כותרות:

[סיכום]
2-4 נקודות קצרות על מה שהוא עשה היום, מבוססות אך ורק על השיחה שלו איתך
היום (מופיעה למטה). אם היום כמעט ריק - שורה אחת שאומרת את זה.

[שאלות]
1-2 שאלות המשך ספציפיות למה שהיום השאיר פתוח - למשל VOC שנעשה ולא נכתב, או
משהו שצריך לדעת על מחר. אם היום ריק לגמרי, שאל את שתי השאלות הקבועות:
האם עשה VOC שלא כתב, והאם יש משהו שכדאי שתדע על היום או על מחר.

כללי ברזל:
- אל תמציא עובדות. כל מה שאתה כותב חייב לבוא מהשיחה למטה, ורק ממנה.
- אל תזכיר כדורגל, משחקים, תוצאות, מניות או מחירים בכלל - חלקים אלה מחושבים
  בנפרד מנתונים מובנים, והטקסט שלך לא יכול להוסיף להם.
- בלי קישורים. עד 8 שורות סך הכל.

--- שיחת היום ---
{history}
"""

_FALLBACK_QUESTIONS = (
    "- עשית היום VOC שלא כתבת? ספר לי וארשום אותו\n"
    "- יש משהו שכדאי שאדע על היום או על מחר?"
)

_HEBREW_DAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")

_NO_RESULTS = "אין תוצאות זמינות כרגע."
_NO_FIXTURES = "אין משחקים קרובים בימים הקרובים."
_NO_STOCKS = (
    "לא הוגדרו מניות למעקב. שלח לי את הסמלים (למשל NVDA.US) "
    "או העבר את דו\"ח התיק מאקסלנס כקובץ אקסל, ואעקוב."
)


def _model_parts(now: datetime, history: str) -> tuple:
    """The two judgement sections, (recap, questions), from the day's one
    model call. On any failure - quota, error, or a reply that ignored the
    format - plain stand-ins: the data sections carry the evening either
    way, and the standing questions need no model."""
    prompt = _PROMPT.format(
        date=f"{now:%Y-%m-%d}",
        history=history or "(אין שיחה מתועדת מהיום)",
    )
    try:
        text = llm.ask(prompt, system=_SYSTEM, max_tokens=500, temperature=0.3)
    except Exception as e:
        # llm.ask already returns None when every provider is spent; this is
        # for the failure its docstring does not promise away.
        logger.error(f"Evening summary model call failed: {e}")
        text = None
    if not text:
        return None, None
    return _parse_model_parts(text)


def _parse_model_parts(text: str) -> tuple:
    """Split the model's reply on its two markers. A reply without them is
    treated as the recap alone - its words still get no route to the data
    sections, so the worst it can do is a thin day summary."""
    recap, questions = text.strip(), None
    if "[שאלות]" in text:
        before, _, after = text.partition("[שאלות]")
        recap, questions = before, after.strip()
    recap = recap.replace("[סיכום]", "").strip()
    return recap or None, questions or None


def build_message(now: datetime, owner_phone: str) -> str:
    """The whole evening message. Never raises: a routine that fails takes the
    tick down with it, and an evening without the summary is better than an
    evening without the reminders that share the tick.

    The assembly is the anti-hallucination boundary: the data sections are
    formatted here, from the feeds, and concatenated with the model's two
    judgement sections. Nothing the model says can add a game, a score, or
    a price to the message."""
    date_he = f"יום {_HEBREW_DAYS[now.weekday()]} {now:%d/%m}"
    football = fetch_football(f"{now:%Y-%m-%d}")
    fixtures = fetch_fixtures(f"{now:%Y-%m-%d}")
    stocks = stocks_section()
    history = _todays_conversation(owner_phone)
    recap, questions = _model_parts(now, history)

    sections = [f"🌙 *סיכום יום - {date_he}*"]
    if recap:
        sections.append(f"📋 *מה עשית היום*\n{recap}")
    sections.append("⚽ *כדורגל*\n" + ("\n".join(football[:3]) if football
                                       else _NO_RESULTS))
    sections.append("🗓️ *משחקים קרובים*\n" + ("\n".join(fixtures) if fixtures
                                               else _NO_FIXTURES))
    sections.append("📈 *מניות*\n" + ("\n".join(stocks) if stocks else _NO_STOCKS))
    sections.append("❓ *לסגור את היום*\n" + (questions or _FALLBACK_QUESTIONS))
    return "\n\n".join(sections)
