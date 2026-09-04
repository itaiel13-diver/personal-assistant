# סטטוס — 2026-09-04

## 🟢 המערכת חיה ועובדת מקצה לקצה

הבוט ענה בפועל בוואטסאפ ב-16:13 UTC. המחזור המלא אומת בלוגים של השרת:

```
16:12:45  שיחת Gemini נפתחה (AFC enabled)
16:13:00  POST .../gemini-3.6-flash:generateContent  →  200 OK
16:13:02  POST /webhook                              →  200
16:13:03  אישור מסירה חזר, זוהה כסטטוס ונענה נכון
```

## מה מחובר

| רכיב | מצב |
|---|---|
| Gemini (`google.genai`, `gemini-3.6-flash`) | ✅ עובד |
| שרת Flask ב-Render | ✅ Live — https://personal-assistant-y754.onrender.com |
| Meta WhatsApp Cloud API — אימות webhook | ✅ עבר (200 על ה-handshake) |
| הרשמה לשדה `messages` | ✅ Subscribed |
| חיבור האפליקציה ל-WABA (`subscribed_apps`) | ✅ `success: true` |
| מספר הטלפון האישי כנמען מאומת | ✅ |
| 23 בדיקות pytest | ✅ ירוקות |

**מזהים:** App ID `1692195671856956` · WABA ID `3170938719759730` · Phone Number ID `1301270403072151` · מספר בדיקה `+1 555 204-1960`

## ⚠️ פעולה נדרשת תוך 24 שעות

**הטוקן הנוכחי (`WHATSAPP_TOKEN`) הוא טוקן זמני ופג תוקף כ-24 שעות אחרי שנוצר** (נוצר 04/09/2026 בערך 18:50 שעון ישראל). כשיפוג — הבוט יקבל הודעות אבל לא יצליח לענות.

**התיקון — יצירת טוקן קבוע:**
1. **business.facebook.com** → Business Settings → **System Users**
2. **Add** → צור/י System User (תפקיד: Admin)
3. **Add Assets** → בחר/י את האפליקציה "עוזר אישי" ואת חשבון ה-WhatsApp
4. **Generate New Token** → בחר/י את האפליקציה → סמן/י הרשאה **`whatsapp_business_messaging`** (ורצוי גם `whatsapp_business_management`)
5. בחר/י תפוגה: **Never**
6. העתק/י את הטוקן → Render → Environment → עדכן/י את `WHATSAPP_TOKEN` → Save

## מגבלות ידועות (לא באגים)

- **Render בטיר החינמי נרדם** אחרי ~15 דקות חוסר פעילות. ההודעה הראשונה אחרי שינה עלולה לקחת 30-60 שניות. מעבר לטיר בתשלום פותר.
- **זיכרון שיחה קצר-טווח מתאפס** בכל הפעלה מחדש של השרת (כולל אחרי שינה). עובדות בזיכרון ארוך-הטווח נשמרות.
- **מספר הבדיקה של Meta מוגבל ל-5 נמענים מאומתים.** למספר עסקי אמיתי צריך Business Verification (Step 3 בממשק).
- **רק הודעות טקסט נתמכות.** הקלטה קולית/תמונה מקבלות תשובה מנומסת שמסבירה זאת.
- `get_itai_targets` ו-`update_daily_schedule` הם עדיין placeholders — אין חיבור אמיתי ל-Google Sheets.

## הצעד הבא המתבקש

חיבור אמיתי ל-Google Sheets, כדי שהעוזר יקרא ויכתוב נתוני יעדים וביקורים אמיתיים במקום הנתונים הקבועים שיש עכשיו. דורש Service Account מ-Google Cloud + שיתוף הגיליון איתו.
