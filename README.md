# Samsung Lowland Assistant

עוזר AI אישי בוואטסאפ עבור איתי, מנהל אזור שפלה ב-Impact Marketing (מותג Samsung).

מבוסס על Gemini (`google-generativeai`) עם Function Calling, ומטרתו לסייע בניהול יומיומי של ~50 נקודות מכירה באזור שפלה (ראשון לציון, רחובות, רמלה, לוד, קרית עקרון, יבנה):

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

## מבנה

- `assistant.py` — הלוגיקה הראשית: System Prompt, כלים (tools) ל-Function Calling, ומנוע הטיפול בהודעות נכנסות.
- `webhook_server.py` — שרת Flask שמקבל הודעות נכנסות מ-Twilio (`POST /whatsapp`), מאמת שהבקשה אכן הגיעה מ-Twilio (חתימה קריפטוגרפית), מעביר את הטקסט ל-`handle_whatsapp_message`, ומחזיר את התשובה כ-TwiML.
- `long_term_memory.json` — נוצר אוטומטית בזמן ריצה, שומר כללים/מיפויים שנלמדו מאיתי. לא נכלל ב-git (ראה `.gitignore`).

## הגדרת Twilio Webhook

1. ב-Twilio Console → **Messaging → Try it out → WhatsApp Sandbox**
2. תחת **"When a message comes in"**, הדבק/י את כתובת ה-webhook הציבורית שלך + `/whatsapp` (למשל `https://your-app.onrender.com/whatsapp`)
3. Method: **HTTP POST**
4. שמור/י — מעכשיו כל הודעה שנשלחת למספר ה-Sandbox תגיע לשרת ותקבל תשובה מהעוזר

## הערות אבטחה

- מפתח ה-API נקרא אך ורק ממשתני סביבה (`GEMINI_API_KEY`) — אין לשמור אותו בקוד.
- העוזר **לא** שולח הודעות/מיילים אוטומטית — תמיד יוצר טיוטה וממתין לאישור מפורש.
- הפונקציות `get_itai_targets` ו-`update_daily_schedule` הן כרגע placeholders — האינטגרציה בפועל מול Google Sheets טרם מומשה.
