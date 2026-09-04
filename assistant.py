import os
import json
import logging
from typing import Dict, Any, List
import google.generativeai as genai

# הגדרת הלוגים למעקב
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. הגדרת מפתח ה-API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

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
    memory_file = "long_term_memory.json"
    memory_data = {}

    if os.path.exists(memory_file):
        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                memory_data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading memory file: {e}")

    memory_data[key] = {
        "value": value,
        "category": category
    }

    try:
        with open(memory_file, "w", encoding="utf-8") as f:
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

# 4. אתחול המודל עם ה-Tools וה-System Prompt
tools_list = [save_to_long_term_memory, get_itai_targets, update_daily_schedule]

model = genai.GenerativeModel(
    model_name="gemini-flash-latest",
    system_instruction=SYSTEM_PROMPT,
    tools=tools_list
)

# 5. מנוע השיחה הראשי
def handle_whatsapp_message(incoming_text: str, chat_history: List[Dict[str, Any]] = None) -> str:
    """
    Handles incoming messages from WhatsApp, maintains chat context,
    and executes function calls automatically if triggered by Gemini.
    """
    # טעינת הזיכרון הקיים לתוך פתיחת השיחה
    memory_context = ""
    if os.path.exists("long_term_memory.json"):
        try:
            with open("long_term_memory.json", "r", encoding="utf-8") as f:
                mem = json.load(f)
                memory_context = f"\n[LONG TERM MEMORY LOADED]: {json.dumps(mem, ensure_ascii=False)}\n"
        except Exception as e:
            logger.error(f"Failed to load memory: {e}")

    full_prompt = f"{memory_context}\nהודעה נכנסת מאיתי: {incoming_text}"

    chat = model.start_chat(enable_automatic_function_calling=True)
    response = chat.send_message(full_prompt)

    return response.text

if __name__ == "__main__":
    print("🤖 העוזר האישי מוכן לפעולה!")
    # בדיקת ניסיון להפעלה מקומית
    # test_response = handle_whatsapp_message("היי, תזכיר לי איזה סניף זה אלם רחובות והאם הוספנו אותו לזיכרון?")
    # print(test_response)
