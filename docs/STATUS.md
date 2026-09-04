# סטטוס — 2026-09-04 (עודכן: מעבר מ-Twilio ל-Meta ישיר)

## שינוי כיוון חשוב

הוחלט לוותר על Twilio לגמרי ולעבור ל-**Meta WhatsApp Business Cloud API ישירות**. הסיבה: הממשק החדש של Twilio ("Tryout UI") לא חשף איפה מגדירים custom webhook, וזה חסם את ההתקדמות. Meta הרשמי הוא גם הבחירה שהומלצה מלכתחילה (חינמי בהיקף התחלתי, בלי middleman).

**כל קוד ה-Twilio הוסר** (`twilio` package, TwiML, X-Twilio-Signature) והוחלף בפרוטוקול האמיתי של Meta.

## מה עובד ואומת בפועל

- **Gemini** — עדיין מחובר ועובד (`gemini-3.6-flash`, `google.genai`), זה לא השתנה.
- **זיכרון שיחה לפי שולח, טיפול בשגיאות, retry** — נשארו זהים, לא הושפעו מהמעבר (הם ב-`assistant.py`, לא תלויים בספק ה-WhatsApp).
- **שרת ה-webhook נכתב מחדש ל-Meta:**
  - `GET /webhook` — handshake אימות חד-פעמי מול Meta (hub.mode/hub.verify_token/hub.challenge)
  - `POST /webhook` — מאמת חתימת Meta (`X-Hub-Signature-256`, HMAC-SHA256), מפרש הודעה נכנסת, ושולח תשובה בקריאה נפרדת ל-Graph API (ל-Meta, בניגוד ל-Twilio, אין תשובה סינכרונית דרך ה-webhook עצמו)
  - מתעלם בבטחה מאירועי סטטוס (delivered/read) ש-Meta שולחת לאותו webhook
- **21 בדיקות pytest ירוקות**, כולל בדיקות ל-Graph API עצמו (URL, headers, payload נכונים) ולטיפול בכשלים (שגיאת API, שגיאת רשת — שניהם לא קורסים).
- **נבדק ב-mutation testing** — שברתי בכוונה גם את בדיקת ה-handshake וגם את בדיקת החתימה, וידאתי שהטסטים אכן נכשלים.

## מה עדיין לא נעשה — צריך אותך

**עוד לא הוקם App ב-Meta for Developers בכלל.** זה דורש כניסה לממשק (developers.facebook.com), לא ניתן לביצוע מהצד שלי. ההוראות המדויקות, שלב-אחר-שלב, ב-`README.md` תחת "הגדרת Meta WhatsApp Cloud API (מהתחלה)". בקצרה:

1. יצירת App + הוספת מוצר WhatsApp → מקבלים `PHONE_NUMBER_ID` + טוקן זמני
2. הוספת מספר הטלפון שלך כנמען מאומת (עד 5 בלי אימות עסקי)
3. חשיפת App Secret
4. חיבור webhook (אחרי שמעדכנים משתני סביבה ב-Render ועושים Redeploy)

## מה עדיין נכון מהסטטוס הקודם

- **פרוס ב-Render:** https://personal-assistant-y754.onrender.com (הקוד יעודכן שם אוטומטית ב-push הבא, בהנחה ש-auto-deploy דלוק — לא מאומת מהצד שלי כי הדומיין חסום מהסביבה שלי)
- **לא נשלח כלום בפועל ללקוחות** — הכל mocks עד כה
- **לא שונו הגדרות תשלום**

## קבצים רלוונטיים

- `assistant.py` — לא השתנה במעבר הזה
- `webhook_server.py` — נכתב מחדש לגמרי ל-Meta
- `tests/test_webhook.py` — נכתב מחדש בהתאם
- `.env.example` — כולל את כל משתני הסביבה החדשים עם הסבר איפה למצוא כל אחד
- `README.md` — הוראות Meta מלאות ומעודכנות
