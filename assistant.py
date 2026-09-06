import os
import json
import logging
import time
from datetime import datetime

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

import storage
from gmail_tools import (
    create_email_draft,
    read_email,
    read_email_attachment,
    search_email_attachment,
    search_emails,
)
from web_tools import (
    read_web_page,
    search_web,
)
from calendar_tools import (
    ISRAEL_TZ,
    create_calendar_event,
    delete_calendar_event,
    get_calendar_events,
    update_calendar_event,
)

# הגדרת הלוגים למעקב
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. הגדרת מפתח ה-API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set in environment variables.")

client = genai.Client(api_key=GEMINI_API_KEY)

# שם המודל ניתן לדריסה דרך משתנה סביבה - שמות מודלים מתיישנים בלי אזהרה
# (ראינו את זה בפועל: gemini-1.5-flash ואז gemini-2.5-flash הפסיקו לעבוד
# באותה שיחת בדיקה אחת), אז אין טעם לקבע אותו עמוק בקוד.
MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.6-flash")

# נתיב מוחלט, לא יחסי ל-CWD - אחרת שינוי בתיקיית ההפעלה (למשל gunicorn
# שמופעל מתיקייה אחרת) גורם לזיכרון להיכתב/להיקרא מהמקום הלא נכון בשקט.
MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "long_term_memory.json")

# 2. ה-System Prompt המקיף והמלא
SYSTEM_PROMPT = """
You are the personal AI operational assistant for Itai, the Lowland Region Manager (מנהל אזור שפלה) at Impact Marketing, representing Samsung.

YOUR IDENTITY & ROLE:
- You operate as Itai's elite executive assistant via WhatsApp.
- Your primary goal is enabling Itai to maximize field efficiency across ~50 sales points (Rishon LeZion, Rehovot, Ramla, Lod, Kiryat Ekron, Yavne), ensure 100% target completion to secure his monthly 1,500 ILS performance bonus, and manage operational tracking files effortlessly.

STRICT SAFETY RULE:
- NEVER send any email or message automatically.
- Always generate drafts (Google Gmail / WhatsApp / SMS) and ask Itai for explicit confirmation before sending or scheduling execution.

ITAI'S 3 CORE RESPONSIBILITIES (כובעי ניהול):
1. Display & POS Compliance (תקינות תצוגה): Verifying screen functionality, replacing broken units, rearranging displays per Samsung guidelines, updating price tags and specs.
2. Staff Training (הדרכות נציגים): Conducting monthly product/feature trainings for sales reps at points of sale.
3. VOC & Feedback (Voice of Customer): Gathering sales insights, promotion feedback, and sales numbers from reps to report to Samsung.

EMAIL ATTACHMENTS:
- read_email lists the files attached to a message; read_email_attachment opens one by name.
- When Itai asks what is in a file someone sent him, or asks a question the attached
  spreadsheet answers, open the attachment rather than answering from the mail body alone.
- Prefer search_email_attachment whenever he wants particular rows rather than the whole
  file - a branch, a city, a person's rows. It scans every row of the file with no limit
  and returns only the hits, so it is both cheaper and more complete than reading.
- Itai's territory is: ראשון לציון, רמלה, לוד, קריית אונו, קריית עקרון, יבנה, אור יהודה.
  When he says "my cities", "my branches" or "the territory", search for all seven. Their
  Hebrew and English spellings are expanded for you - pass the plain city name.
- To cross-reference cities with people, put the cities in keywords and the names in
  must_also_match. Ask him for the exact spelling of a name only if a search comes back
  empty and you suspect the spelling.
- NEVER stop early or say a file is too long to finish. read_email_attachment returns
  numbered parts and tells you how many there are - if he asks for the whole file, keep
  calling it with part=2, part=3 and so on until the last part, then answer from all of
  them. Saying "the rest is hidden" is only correct if you have actually fetched every part.
- Images, scanned PDFs and old .xls/.doc files cannot be read. Say so plainly and say what
  would fix it (re-saving as xlsx/docx) - never guess at contents you could not read.

THE INTERNET:
- search_web looks something up live; read_web_page opens one specific address.
- Your training data has a cutoff and Itai has no idea where it falls. Anything that
  can change - today's news, a price, a score, opening hours, whether a product still
  exists, what a company is doing now - goes through a tool. Answering it from memory
  produces something that sounds current and is not, which is the worst failure you
  have available.
- If he sends a link, open it with read_web_page rather than guessing from the address.
- Live search may come back saying it is unavailable on the current plan. That is a
  real answer, not an error to hide: tell him, and offer to open a specific link
  instead. Do not quietly answer from memory in its place.
- Never use these for his own mail, calendar or files - those have their own tools and
  the web does not know about them.

DATA EXTRACTION & FILE HANDLING RULES:
1. Strict Context Filtering:
   - The connected Google Sheets contain data for multiple regions and managers.
   - You MUST filter every query, report, and calculation EXCLUSIVELY by:
     * Region: "שפלה" (Lowland)
     * Manager Name: "איתי" (Itai)
   - NEVER process, summarize, or output data belonging to other regions or managers.
2. Entity Matching & Normalization:
   - Match informal store names sent by Itai (e.g., "אל"ם רחובות", "KSP עקרון") to their exact formal structure in the master sheet using your memory or function tools.

ACTIVE LEARNING, NO-GUESSING & LONG-TERM MEMORY RULES:
1. Strict No-Guessing Policy:
   - If you encounter missing data, ambiguous terms, unknown store codes, or unclear guidelines, NEVER guess or assume. Ask Itai directly for clarification.
2. Proactive Knowledge Gathering:
   - Ask concise, targeted questions whenever there is an opportunity to improve operational efficiency.
3. Memory Updating Trigger:
   - When Itai answers a clarification question or gives a new rule/mapping, call `save_to_long_term_memory(key, value, category)` immediately to save it permanently.

SCHEDULE & ROUTINES:
- Daily Morning Briefing (08:30 AM): Proposed daily schedule, optimized route, Waze links, monthly target status, open tasks.
- Shift Sign-In/Out (08:55 AM & 17:55 PM): Connecteam shift reminders.
- Weekly Smart Bonus Reminder (Thursdays at 16:00 PM): Progress towards 1,500 ILS bonus, lagging targets, and next week's recommended priorities.

COMMUNICATION STYLE:
- Natural, sharp, highly structured Israeli business Hebrew.
- Use bolding (**text**) and bullet points for readability on mobile/while driving.
- Concise and action-oriented.
"""

# 3. הגדרת פונקציות ה-Tools (Function Calling)

def save_to_long_term_memory(key: str, value: str, category: str = "general") -> str:
    """Saves a new learned rule, store mapping, or preference permanently."""
    if storage.enabled():
        try:
            storage.save_memory(key, value, category)
            return f"✅ הזיכרון עודכן בהצלחה: {key} = {value}"
        except Exception as e:
            return f"❌ שגיאה בשמירת הזיכרון: {str(e)}"

    memory_data = {}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memory_data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading memory file: {e}")

    memory_data[key] = {"value": value, "category": category}

    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory_data, f, ensure_ascii=False, indent=2)
        return f"✅ הזיכרון עודכן בהצלחה: {key} = {value}"
    except Exception as e:
        return f"❌ שגיאה בשמירת הזיכרון: {str(e)}"

def get_itai_targets(month: str = "current") -> str:
    """Fetches the monthly targets for Lowland region (אזור שפלה) and Itai from Google Sheets."""
    # כאן ייכנס הקוד הייעודי מול Google Sheets API דרך Claude Code
    return json.dumps({
        "status": "success",
        "manager": "איתי",
        "region": "שפלה",
        "required_visits_this_week": 12,
        "completed_visits": 8,
        "bonus_eligibility_pace": "83%"
    }, ensure_ascii=False)

def update_daily_schedule(store_name: str, status: str, notes: str) -> str:
    """Updates the actual store visit status and notes in the daily schedule file."""
    # כאן ייכנס הקוד הייעודי לעדכון שורה ב-Google Sheets
    return f"✅ עודכן בהצלחה בלו\"ז: ביקור ב-{store_name} מסומן כ-{status}."

tools_list = [
    save_to_long_term_memory,
    get_itai_targets,
    update_daily_schedule,
    get_calendar_events,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event,
    search_emails,
    read_email,
    read_email_attachment,
    search_email_attachment,
    create_email_draft,
    search_web,
    read_web_page,
]


def _load_memory_context() -> str:
    """Renders long-term memory as extra system context, injected on every message."""
    try:
        if storage.enabled():
            mem = storage.load_memory()
        elif os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                mem = json.load(f)
        else:
            return ""
        if not mem:
            return ""
        return f"\n\n[LONG TERM MEMORY]: {json.dumps(mem, ensure_ascii=False)}"
    except Exception as e:
        logger.error(f"Failed to load memory: {e}")
        return ""


# 4. שיחות מתמשכות לפי שולח
# אין כאן מצב בזיכרון התהליך בכוונה. כל הודעה טוענת את היסטוריית השיחה
# ממסד הנתונים, בונה ממנה שיחה חדשה, ושומרת את ההיסטוריה המעודכנת בחזרה.
# זה מה שמאפשר לעוזר לזכור גם אחרי שהשרת נרדם, קרס או נפרס מחדש - התרחיש
# שגרם לתחושה של "שיחה עם מישהו שלא זוכר כלום".
# כשאין מסד נתונים מוגדר, המילון הזה משמש כגיבוי - והוא אכן נמחק בכל הפעלה מחדש.
_fallback_sessions: dict[str, tuple] = {}


def _retry_on_server_error(fn, attempts: int = 3):
    """Gemini's servers return transient 503s under load - retry with backoff before giving up.
    Used for every call to Gemini (session creation included) - a failure creating a brand new
    session is exactly as real a failure mode as one sending a message on an existing session."""
    delay_seconds = 2
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except genai_errors.ServerError as e:
            last_error = e
            logger.warning(f"Gemini ServerError, attempt {attempt}/{attempts}: {e}")
            if attempt < attempts:
                time.sleep(delay_seconds)
                delay_seconds *= 2
    raise last_error


def _date_context() -> str:
    """Without this the model has no idea what 'today' or 'tomorrow' mean, and would
    schedule calendar events on arbitrary dates."""
    now = datetime.now(ISRAEL_TZ)
    return f"\n\n[CURRENT DATE AND TIME IN ISRAEL]: {now.strftime('%A, %d/%m/%Y, %H:%M')}"


def _build_config() -> types.GenerateContentConfig:
    """Rebuilt for every message so the date and the long-term memory are always
    current, however old the stored conversation is."""
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT + _load_memory_context() + _date_context(),
        tools=tools_list,
    )


def _serialise(history: list) -> list:
    return [c.model_dump(mode="json", exclude_none=True) for c in history]


def _deserialise(raw: list) -> list:
    restored = []
    for entry in raw:
        try:
            restored.append(types.Content(**entry))
        except Exception as e:
            # One malformed row must not lock the sender out of their whole history.
            logger.error(f"Dropping unreadable history entry: {e}")
    return restored


def _get_session(sender_id: str):
    """Returns a chat rehydrated from stored history. Without a database this
    degrades to a per-process cache that a restart wipes."""
    if storage.enabled():
        history = _deserialise(storage.load_history(sender_id))
        return _retry_on_server_error(lambda: client.chats.create(
            model=MODEL_NAME, config=_build_config(), history=history
        ))

    today = datetime.now(ISRAEL_TZ).date()
    cached = _fallback_sessions.get(sender_id)
    if cached is None or cached[1] != today:
        chat = _retry_on_server_error(lambda: client.chats.create(
            model=MODEL_NAME, config=_build_config()
        ))
        _fallback_sessions[sender_id] = (chat, today)
    return _fallback_sessions[sender_id][0]


def _send_with_retry(chat, text: str, attempts: int = 3):
    return _retry_on_server_error(lambda: chat.send_message(text), attempts=attempts)


# 5. מנוע השיחה הראשי
def handle_whatsapp_message(incoming_text: str, sender_id: str = "default") -> str:
    """
    Handles an incoming WhatsApp message from a given sender, restoring the
    conversation from storage so it survives restarts, and executing function
    calls automatically when Gemini triggers them.
    """
    try:
        chat = _get_session(sender_id)
        response = _send_with_retry(chat, incoming_text)
        if storage.enabled():
            storage.save_history(sender_id, _serialise(chat.get_history()))
    except genai_errors.ClientError as e:
        logger.error(f"Gemini client error for sender {sender_id}: {e}")
        # A 429 on the free tier is a daily quota that will not clear on a retry,
        # so telling the sender "temporary, try again in a moment" would be a lie.
        if getattr(e, "code", None) == 429:
            return "נגמרה מכסת השימוש היומית ב-AI. היא מתאפסת מחר, או שאפשר לשדרג את התוכנית."
        return "מצטער, יש תקלה בחיבור ל-AI. נסה/י שוב בעוד רגע."
    except Exception as e:
        logger.error(f"Gemini call failed for sender {sender_id}: {e}")
        return "מצטער, יש כרגע תקלה זמנית בחיבור ל-AI. נסה/י שוב בעוד רגע."
    # response.text is None (not an exception) when there are no text parts -
    # e.g. a safety-blocked response. Sending None onward would reach the
    # WhatsApp API as a null body and fail silently, leaving the sender with
    # no reply and no clue why.
    return response.text or "לא הצלחתי לייצר תשובה להודעה הזו. אפשר לנסח את זה קצת אחרת?"

if __name__ == "__main__":
    print("🤖 העוזר האישי מוכן לפעולה!")
    # בדיקת ניסיון להפעלה מקומית
    # test_response = handle_whatsapp_message("היי, תזכיר לי איזה סניף זה אלם רחובות והאם הוספנו אותו לזיכרון?", "local-test")
    # print(test_response)
