import os
import json
import logging
import time

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

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

MEMORY_FILE = "long_term_memory.json"

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
    """Saves a new learned rule, store mapping, or preference into the long-term memory JSON file."""
    memory_data = {}

    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memory_data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading memory file: {e}")

    memory_data[key] = {
        "value": value,
        "category": category
    }

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

tools_list = [save_to_long_term_memory, get_itai_targets, update_daily_schedule]


def _load_memory_context() -> str:
    """Renders long-term memory as extra system context, for injection when a session starts."""
    if not os.path.exists(MEMORY_FILE):
        return ""
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            mem = json.load(f)
        if not mem:
            return ""
        return f"\n\n[LONG TERM MEMORY]: {json.dumps(mem, ensure_ascii=False)}"
    except Exception as e:
        logger.error(f"Failed to load memory: {e}")
        return ""


# 4. שיחות מתמשכות לפי שולח
# כל שולח (מספר וואטסאפ) מקבל אובייקט Chat אחד שנשמר לאורך חיי התהליך, כך
# שהעוזר זוכר את מהלך השיחה הנוכחי ולא רק עובדות מהזיכרון ארוך-הטווח.
# הערה: זה זיכרון בתוך-תהליך בלבד - הוא מתאפס בכל הפעלה מחדש של השרת
# (למשל Render בטיר החינמי שנרדם אחרי חוסר פעילות).
_sessions: dict[str, "genai.chats.Chat"] = {}


def _get_session(sender_id: str):
    if sender_id not in _sessions:
        _sessions[sender_id] = client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT + _load_memory_context(),
                tools=tools_list,
            ),
        )
    return _sessions[sender_id]


def _send_with_retry(chat, text: str, attempts: int = 3):
    """Gemini's servers return transient 503s under load - retry with backoff before giving up."""
    delay_seconds = 2
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return chat.send_message(text)
        except genai_errors.ServerError as e:
            last_error = e
            logger.warning(f"Gemini ServerError, attempt {attempt}/{attempts}: {e}")
            if attempt < attempts:
                time.sleep(delay_seconds)
                delay_seconds *= 2
    raise last_error


# 5. מנוע השיחה הראשי
def handle_whatsapp_message(incoming_text: str, sender_id: str = "default") -> str:
    """
    Handles an incoming WhatsApp message from a given sender, keeping a
    persistent multi-turn conversation per sender, and executes function
    calls automatically when Gemini triggers them.
    """
    chat = _get_session(sender_id)
    try:
        response = _send_with_retry(chat, incoming_text)
    except Exception as e:
        logger.error(f"Gemini call failed for sender {sender_id}: {e}")
        return "מצטער, יש כרגע תקלה זמנית בחיבור ל-AI. נסה/י שוב בעוד רגע."
    return response.text

if __name__ == "__main__":
    print("🤖 העוזר האישי מוכן לפעולה!")
    # בדיקת ניסיון להפעלה מקומית
    # test_response = handle_whatsapp_message("היי, תזכיר לי איזה סניף זה אלם רחובות והאם הוספנו אותו לזיכרון?", "local-test")
    # print(test_response)
