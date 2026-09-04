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

## הפעלת שרת ה-Webhook (וואטסאפ דרך Twilio)

```bash
pip install -r requirements.txt
cp .env.example .env   # ומלא/י GEMINI_API_KEY ו-TWILIO_AUTH_TOKEN
python webhook_server.py       # לפיתוח מקומי (Flask dev server, פורט 5000)
# בייצור:
gunicorn webhook_server:app --bind 0.0.0.0:$PORT
```

לחיבור מקומי ל-Twilio Sandbox לצורך בדיקה (בלי לפרוס לענן) אפשר להשתמש ב-[ngrok](https://ngrok.com) כדי לחשוף את הפורט המקומי ל-URL ציבורי זמני, ולהגדיר אותו כ-webhook ב-Twilio Console.

## הרצת הבדיקות

```bash
pip install -r requirements-dev.txt
pytest -v
```

כל הבדיקות עובדות מול Mocks בלבד (לא קוראות בפועל ל-Gemini או ל-Twilio), כך שהן רצות מהר ובלי תלות ברשת או במפתחות אמיתיים.

## מבנה

- `assistant.py` — הלוגיקה הראשית: System Prompt, כלים (tools) ל-Function Calling, וניהול שיחה מתמשכת לפי שולח.
- `webhook_server.py` — שרת Flask שמקבל הודעות נכנסות מ-Twilio (`POST /whatsapp`), מאמת שהבקשה אכן הגיעה מ-Twilio (חתימה קריפטוגרפית), מעביר את הטקסט + מזהה השולח ל-`handle_whatsapp_message`, ומחזיר את התשובה כ-TwiML.
- `tests/` — בדיקות pytest ל-webhook (אימות חתימה, ניתוב) ול-assistant (זיכרון ארוך-טווח, ניהול שיחות לפי שולח, retry).
- `long_term_memory.json` — נוצר אוטומטית בזמן ריצה, שומר כללים/מיפויים שנלמדו מאיתי. לא נכלל ב-git (ראה `.gitignore`).

## זיכרון שיחה — איך זה עובד בפועל

יש שני סוגי זיכרון שונים, בכוונה:

1. **זיכרון שיחה (קצר-טווח, לפי שולח)** — כל מספר וואטסאפ מקבל אובייקט שיחה (`Chat`) משלו שנשמר בזיכרון התהליך (`_sessions` ב-`assistant.py`) לאורך כל משך ריצת השרת. כך העוזר זוכר את הקשר השיחה הנוכחית ("מה אמרתי לך במסר הקודם"), לא רק עובדות בודדות.
2. **זיכרון ארוך-טווח (`long_term_memory.json`)** — עובדות/מיפויים שנלמדו במפורש דרך `save_to_long_term_memory`, ונטענים כהקשר בתחילת כל שיחה חדשה (כולל לשולחים אחרים).

**מגבלה חשובה:** זיכרון השיחה הקצר-טווח הוא **בתוך-תהליך בלבד** — הוא מתאפס בכל הפעלה מחדש של השרת. ב-Render בטיר החינמי, השירות "נרדם" אחרי חוסר פעילות וקם מחדש בבקשה הבאה — כלומר שיחות ארוכות שנעצרות לזמן מה עלולות "לאבד" את ההקשר הקצר-טווח (העובדות בזיכרון הארוך-טווח כן נשמרות, כי הן על הדיסק). אם זה יהפוך לבעיה בפועל, הפתרון הוא זיכרון שיחה מבוסס-DB חיצוני (למשל Redis או טבלה ב-D1/Postgres) במקום דיקשנרי בזיכרון.

## הגדרת Twilio Webhook

1. ב-Twilio Console → **Messaging → Try it out → WhatsApp Sandbox**
2. תחת **"When a message comes in"**, הדבק/י את כתובת ה-webhook הציבורית שלך + `/whatsapp` (למשל `https://your-app.onrender.com/whatsapp`)
3. Method: **HTTP POST**
4. שמור/י — מעכשיו כל הודעה שנשלחת למספר ה-Sandbox תגיע לשרת ותקבל תשובה מהעוזר

**הערה (2026-09-04):** Twilio עדכנו את הממשק ל-"Tryout UI" חדש; מיקום הגדרת ה-webhook השתנה מהמסך הישן ("Sandbox settings"). עדיין לא אותר המיקום המדויק בממשק החדש — זה הפריט הפתוח היחיד שמונע חיבור מלא בפועל. ראה `docs/STATUS.md`.

## הערות אבטחה

- מפתחות ה-API (`GEMINI_API_KEY`, `TWILIO_AUTH_TOKEN`) נקראים אך ורק ממשתני סביבה — אין לשמור אותם בקוד, ו-`.env` מוגן ב-`.gitignore`.
- כל בקשת `POST /whatsapp` מאומתת מול חתימת Twilio (`X-Twilio-Signature`) — בקשה מזויפת נדחית עם 403 לפני שהיא מגיעה ל-Gemini.
- העוזר **לא** שולח הודעות/מיילים אוטומטית — תמיד יוצר טיוטה וממתין לאישור מפורש.
- הפונקציות `get_itai_targets` ו-`update_daily_schedule` הן כרגע placeholders — האינטגרציה בפועל מול Google Sheets טרם מומשה.
- קריאות ל-Gemini עוברות retry אוטומטי (עד 3 ניסיונות, backoff מעריכי) על שגיאות זמניות (503); כשל מתמשך מחזיר הודעת שגיאה בעברית במקום קריסה שקטה.
