# Samsung Lowland Assistant

עוזר AI אישי בוואטסאפ עבור איתי, מנהל אזור שפלה ב-Impact Marketing (מותג Samsung).

מבוסס על Gemini (`google-genai`) עם Function Calling וזיכרון שיחה לפי שולח, ומטרתו לסייע בניהול יומיומי של ~50 נקודות מכירה באזור שפלה (ראשון לציון, רחובות, רמלה, לוד, קרית עקרון, יבנה):

- **תקינות תצוגה** — מעקב אחר מסכים, החלפת יחידות תקולות, סידור תצוגה לפי הנחיות Samsung.
- **הדרכות נציגים** — מעקב אחר הדרכות מוצר חודשיות בנקודות המכירה.
- **VOC ומשוב** — איסוף תובנות מכירה, משוב על מבצעים ונתוני מכירות מנציגים.

## הפעלה מקומית — בדיקת הלוגיקה בלבד

```bash
pip install -r requirements.txt
cp .env.example .env   # ומלא/י את GEMINI_API_KEY
python assistant.py
```

## הפעלת שרת ה-Webhook (וואטסאפ דרך Meta WhatsApp Cloud API)

```bash
pip install -r requirements.txt
cp .env.example .env   # ומלא/י GEMINI_API_KEY, WHATSAPP_TOKEN, PHONE_NUMBER_ID, META_APP_SECRET, META_VERIFY_TOKEN
python webhook_server.py       # לפיתוח מקומי (Flask dev server, פורט 5000)
# בייצור:
gunicorn webhook_server:app --bind 0.0.0.0:$PORT
```

## הרצת הבדיקות

```bash
pip install -r requirements-dev.txt
pytest -v
```

כל הבדיקות עובדות מול Mocks בלבד (לא קוראות בפועל ל-Gemini או ל-Meta), כך שהן רצות מהר ובלי תלות ברשת או במפתחות אמיתיים.

## מבנה

- `assistant.py` — הלוגיקה הראשית: System Prompt, כלים (tools) ל-Function Calling, וניהול שיחה מתמשכת לפי שולח.
- `webhook_server.py` — שרת Flask שמדבר את פרוטוקול WhatsApp Cloud API של Meta: `GET /webhook` לאימות חד-פעמי (handshake), `POST /webhook` לקבלת הודעות נכנסות (מאומת מול חתימת Meta - `X-Hub-Signature-256`), ושליחת תשובה בקריאה נפרדת ומפורשת ל-Graph API (בניגוד ל-Twilio, ל-Meta אין מנגנון תשובה סינכרוני דרך תגובת ה-webhook עצמה).
- `calendar_tools.py` — חיבור ל-Google Calendar דרך Service Account: קריאת אירועים וקביעת אירועים חדשים, הכל באזור זמן ישראל.
- `tests/` — בדיקות pytest ל-webhook (handshake, אימות חתימה, שליחה בפועל ל-Graph API, טיפול באירועי סטטוס), ל-assistant (זיכרון ארוך-טווח, ניהול שיחות לפי שולח, retry, רענון יומי של הסשן) וליומן (פורמט אירועים, אזור זמן, טיפול בשגיאות).
- `long_term_memory.json` — נוצר אוטומטית בזמן ריצה, שומר כללים/מיפויים שנלמדו מאיתי. לא נכלל ב-git (ראה `.gitignore`).

## זיכרון שיחה — איך זה עובד בפועל

יש שני סוגי זיכרון שונים, בכוונה:

1. **זיכרון שיחה (קצר-טווח, לפי שולח)** — כל מספר וואטסאפ מקבל אובייקט שיחה (`Chat`) משלו שנשמר בזיכרון התהליך (`_sessions` ב-`assistant.py`) לאורך כל משך ריצת השרת. כך העוזר זוכר את הקשר השיחה הנוכחית ("מה אמרתי לך במסר הקודם"), לא רק עובדות בודדות.
2. **זיכרון ארוך-טווח (`long_term_memory.json`)** — עובדות/מיפויים שנלמדו במפורש דרך `save_to_long_term_memory`, ונטענים כהקשר בתחילת כל שיחה חדשה (כולל לשולחים אחרים).

**מגבלה חשובה:** זיכרון השיחה הקצר-טווח הוא **בתוך-תהליך בלבד** — הוא מתאפס בכל הפעלה מחדש של השרת. ב-Render בטיר החינמי, השירות "נרדם" אחרי חוסר פעילות וקם מחדש בבקשה הבאה — כלומר שיחות ארוכות שנעצרות לזמן מה עלולות "לאבד" את ההקשר הקצר-טווח (העובדות בזיכרון הארוך-טווח כן נשמרות, כי הן על הדיסק). אם זה יהפוך לבעיה בפועל, הפתרון הוא זיכרון שיחה מבוסס-DB חיצוני (למשל Redis או טבלה ב-D1/Postgres) במקום דיקשנרי בזיכרון.

## הגדרת Meta WhatsApp Cloud API (מהתחלה)

### שלב 1 — יצירת App ב-Meta for Developers

1. **https://developers.facebook.com/apps** → **Create App** → סוג **"Business"**
2. בתוך ה-App, הוסף/י את המוצר **WhatsApp**
3. במסך **WhatsApp → API Setup** תראה/י:
   - **מספר טלפון בדיקה זמני** (Test number) — מוכן לשימוש מיידי, בלי אימות עסקי
   - **Phone Number ID** — זה ה-`PHONE_NUMBER_ID`
   - **Temporary Access Token** (תוקף 24 שעות — טוב לבדיקה ראשונית בלבד, ראה שלב 4 לטוקן קבוע)
4. באותו מסך, תחת **"To"**, הוסף/י את מספר הוואטסאפ שלך כ-**נמען מאומת** (Add recipient phone number) — עד 5 מספרים אפשריים בלי אימות עסקי, וזה הכרחי כדי שתוכל/י לקבל הודעות מהעוזר בשלב הבדיקה.

### שלב 2 — App Secret

**App Settings → Basic** → ליד "App Secret" לחץ/י **Show** (יבקש סיסמה) → זה ה-`META_APP_SECRET`.

### שלב 3 — חיבור ה-Webhook (אחרי שהשרת פרוס ורץ)

1. **WhatsApp → Configuration**
2. **Edit** ליד Webhook
3. **Callback URL:** `https://personal-assistant-y754.onrender.com/webhook`
4. **Verify token:** כל מחרוזת שתבחר/י בעצמך (למשל `samsung-assistant-2026`) — **תעדכן/י את אותה מחרוזת גם ב-`META_VERIFY_TOKEN`** ב-Render (Environment Variables), ואז **Redeploy** לפני שתלחץ/י Verify כאן, אחרת האימות ייכשל
5. לחץ/י **Verify and Save**
6. תחת **Webhook fields**, סמן/י **Subscribe** ליד `messages`

### שלב 4 — טוקן קבוע (כשה-24 שעות של הטוקן הזמני נגמרות)

**Business Settings → System Users → Add** → צור/י System User → **Generate Token**, בחר/י את ה-App, ותן/י הרשאת `whatsapp_business_messaging` → זה טוקן שלא פג תוקף (בניגוד לטוקן הזמני). עדכן/י את `WHATSAPP_TOKEN` בהתאם.

## הגדרת Google Calendar

העוזר קורא וכותב ליומן דרך **Service Account** — לא דרך התחברות אישית, כך שאין טוקן שפג תוקף.

1. **Google Cloud Console** → הפעל/י את **Google Calendar API**
2. **IAM & Admin → Service Accounts** → צור/י Service Account (בלי תפקידים ברמת הפרויקט)
3. בלשונית **Keys** → Add Key → **JSON** → יורד קובץ
4. תוכן הקובץ כולו נכנס כמשתנה סביבה `GOOGLE_SERVICE_ACCOUNT_JSON` (שורה אחת)
5. **calendar.google.com** → היומן הרצוי → ⋮ → הגדרות ושיתוף → **שיתוף עם אנשים ספציפיים** → הוסף/י את כתובת ה-Service Account עם הרשאת **"שינויים באירועים"**
6. `GOOGLE_CALENDAR_ID` = כתובת בעל היומן ששיתפת

**שתי מלכודות שנתקלנו בהן בפועל:**
- ל-Service Account יש יומן `primary` **משלו** (ריק). חייבים לציין `GOOGLE_CALENDAR_ID` מפורשות, אחרת קוראים יומן ריק.
- `calendarList` של Service Account נשאר **ריק** גם אחרי שיתוף תקין — זה לא סימן לתקלה. גישה ישירה לפי מזהה היומן עובדת בכל זאת.

## הערות אבטחה

- מפתחות ה-API (`GEMINI_API_KEY`, `WHATSAPP_TOKEN`, `META_APP_SECRET`) נקראים אך ורק ממשתני סביבה — אין לשמור אותם בקוד, ו-`.env` מוגן ב-`.gitignore`.
- כל בקשת `POST /webhook` מאומתת מול חתימת Meta (`X-Hub-Signature-256`, HMAC-SHA256 עם ה-App Secret) — בקשה מזויפת נדחית עם 403 לפני שהיא מגיעה ל-Gemini.
- העוזר **לא** שולח הודעות/מיילים אוטומטית — תמיד יוצר טיוטה וממתין לאישור מפורש.
- הפונקציות `get_itai_targets` ו-`update_daily_schedule` הן כרגע placeholders — האינטגרציה בפועל מול Google Sheets טרם מומשה.
- קריאות ל-Gemini עוברות retry אוטומטי (עד 3 ניסיונות, backoff מעריכי) על שגיאות זמניות (503); כשל מתמשך מחזיר הודעת שגיאה בעברית במקום קריסה שקטה.
