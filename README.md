# Samsung Lowland Assistant

עוזר AI אישי בוואטסאפ עבור איתי, מנהל אזור שפלה ב-Impact Marketing (מותג Samsung).

מבוסס על Gemini (`google-generativeai`) עם Function Calling, ומטרתו לסייע בניהול יומיומי של ~50 נקודות מכירה באזור שפלה (ראשון לציון, רחובות, רמלה, לוד, קרית עקרון, יבנה):

- **תקינות תצוגה** — מעקב אחר מסכים, החלפת יחידות תקולות, סידור תצוגה לפי הנחיות Samsung.
- **הדרכות נציגים** — מעקב אחר הדרכות מוצר חודשיות בנקודות המכירה.
- **VOC ומשוב** — איסוף תובנות מכירה, משוב על מבצעים ונתוני מכירות מנציגים.

## הפעלה מקומית

```bash
pip install -r requirements.txt
cp .env.example .env   # ומלא/י את GEMINI_API_KEY
python assistant.py
```

## מבנה

- `assistant.py` — הלוגיקה הראשית: System Prompt, כלים (tools) ל-Function Calling, ומנוע הטיפול בהודעות נכנסות.
- `long_term_memory.json` — נוצר אוטומטית בזמן ריצה, שומר כללים/מיפויים שנלמדו מאיתי. לא נכלל ב-git (ראה `.gitignore`).

## הערות אבטחה

- מפתח ה-API נקרא אך ורק ממשתני סביבה (`GEMINI_API_KEY`) — אין לשמור אותו בקוד.
- העוזר **לא** שולח הודעות/מיילים אוטומטית — תמיד יוצר טיוטה וממתין לאישור מפורש.
- הפונקציות `get_itai_targets` ו-`update_daily_schedule` הן כרגע placeholders — האינטגרציה בפועל מול Google Sheets טרם מומשה.
