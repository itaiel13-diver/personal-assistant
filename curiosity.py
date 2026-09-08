"""One question a day: how the assistant stops being as ignorant tomorrow.

The assistant's instructions have always told it to ask when it does not know
something, and it never does - because a question only occurs to it while it is
already answering a message, and by then the useful thing is the answer, not
the interrogation. So the knowledge it needs to be worth anything (which store
is which, who is who, how the work is actually organised) has stayed in Itai's
head, and every request has been answered a little more vaguely than it could
have been.

This module asks one question a day and nothing more. The rules that make that
tolerable rather than annoying:

- One a day, at a fixed hour, on working days. A question is the least urgent
  thing this assistant ever sends, so it never competes with a reminder.
- Each question is asked once, ever. If he does not answer, that is an answer:
  the question is claimed in the proactive log the moment it goes out and never
  comes back. An assistant that repeats a question he ignored is a nag, and he
  would stop reading all of them.
- A question is only asked while its answer is still missing. The seeds below
  are keyed to long-term memory, so anything he has already told the assistant
  - in a question like this or in the middle of an ordinary conversation -
  drops out of the queue without anyone maintaining a list.
- Answering costs one sentence. No question here needs a paragraph, and none
  of them asks him to look anything up.

The seeds run out, deliberately: about three weeks of questions, which is
roughly the point at which the assistant knows the territory and further
questions should come from what it has actually seen rather than from what I
guessed in advance. After that it asks the model for one, given everything it
already knows - and with no model key configured it simply goes quiet, which is
the correct behaviour for a feature whose whole value is being occasionally
useful.
"""
import json
import logging
import os
import re
from dataclasses import dataclass

import llm
import storage

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "long_term_memory.json")

# A memory key the model invents has to be storable and matchable, so it is
# held to the same shape as the seeded ones.
_KEY = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


@dataclass(frozen=True)
class Question:
    """key is where the answer will live in long-term memory, and is also what
    makes the question stop being asked once it is answered."""
    key: str
    text: str
    category: str = "general"


# The seven cities of Itai's territory, first, because they are the gap that
# shows up in almost every request: he says "the Rishon store" and the
# assistant has no idea which of them that is.
_TERRITORY = (
    ("store_rishon_lezion", "ראשון לציון"),
    ("store_ramla", "רמלה"),
    ("store_lod", "לוד"),
    ("store_kiryat_ono", "קריית אונו"),
    ("store_kiryat_ekron", "קריית עקרון"),
    ("store_yavne", "יבנה"),
    ("store_or_yehuda", "אור יהודה"),
)

SEEDS = tuple(
    Question(key, f"איך קוראים בדיוק לחנות שאתה מכסה ב{city}? (השם המלא, כמו שהוא מופיע בדוחות)", "territory")
    for key, city in _TERRITORY
) + (
    Question("territory_cities", "יש עוד ערים או סניפים באזור שלך מעבר לשבע שאני מכיר? אילו?", "territory"),
    Question("my_full_name", "איך השם שלך כתוב בדוחות של סמסונג — בעברית ובאנגלית?", "identity"),
    Question("nikita_full_name", "איך השם של ניקיטה כתוב בדוחות? רוצה לזהות אותו נכון כשאני קורא קובץ.", "identity"),
    Question("my_manager", "מי המנהל הישיר שלך, ומה הכתובת שממנה הוא שולח לך מיילים?", "people"),
    Question("my_role_title", "מה התפקיד המדויק שלך, כמו שהיית כותב אותו במייל רשמי?", "identity"),
    Question("weekly_report", "יש דוח קבוע שאתה מגיש כל שבוע? מתי, ולמי?", "routine"),
    Question("work_hours", "מה שעות העבודה הרגילות שלך, ובאילו ימים אתה בשטח לעומת מהבית?", "routine"),
    Question("urgent_senders", "ממי מייל נחשב אצלך דחוף תמיד, גם אם הנושא נראה שגרתי?", "people"),
    Question("ignore_senders", "יש שולחים שאתה אף פעם לא רוצה שאציק לך עליהם? תן לי כתובות או שמות.", "people"),
    Question("product_lines", "על אילו קווי מוצר אתה אחראי בפועל? (מסכים, מקררים, מכונות כביסה...)", "work"),
    Question("visit_cycle", "כל כמה זמן אתה אמור לבקר בכל חנות, ואיך אתה מתעד את זה?", "routine"),
    Question("competitors", "מול אילו מותגים מתחרים אתה נמדד בשטח?", "work"),
    Question("connecteam_notes", "חוץ מרישום כניסה ויציאה, יש עוד משהו שאתה חייב לדווח ב-Connecteam?", "routine"),
    Question("expense_process", "איך אתה מגיש החזר הוצאות, ומה הדדליין החודשי?", "routine"),
    Question("car_and_travel", "אתה נוסע ברכב חברה? יש משהו שאני צריך לזכור לגבי דלק, כביש 6 או חניה?", "routine"),
    Question("training_files", "קובץ הסטטוס של ההדרכות — איפה הוא יושב, ומי מעדכן אותו?", "work"),
    Question("escalation_path", "כשמשהו נתקע בחנות ואתה צריך שמישהו יזוז — למי אתה פונה קודם?", "people"),
    Question("quiet_hours", "יש שעות שבהן אתה מעדיף שלא אשלח לך כלום?", "preferences"),
    Question("hebrew_or_english", "כשאני מסכם לך מייל באנגלית — לתרגם לעברית או להשאיר במקור?", "preferences"),
)

_SYSTEM = (
    "You help a personal assistant learn about the person it works for. "
    "He is Itai, a field region manager for Samsung home appliances and displays in Israel, "
    "covering stores in Rishon LeZion, Ramla, Lod, Kiryat Ono, Kiryat Ekron, Yavne and Or Yehuda. "
    "You are given everything the assistant already knows about him. "
    "Propose exactly ONE short question, in Hebrew, whose answer would make the assistant "
    "meaningfully more useful to him tomorrow. "
    "Rules: ask about something the assistant does NOT already know; ask something he can answer "
    "in one sentence from memory, with nothing to look up; ask about his work, his people, his "
    "territory or how he wants to be helped - never about his private life, his health, his family "
    "or his money. "
    'Answer with JSON only, in the form {"key": "snake_case_english_key", "question": "...", "category": "..."}. '
    "The key must be lowercase English words joined by underscores, and must not be one of the keys you were given."
)


def _memory() -> dict:
    """Long-term memory, from wherever it actually lives.

    Mirrors assistant._load_memory_context: the database is the real store, and
    the file is the fallback for a machine running without one. Read here
    rather than imported, because importing the assistant module from a routine
    would pull the whole Gemini client into the heartbeat.
    """
    try:
        if storage.enabled():
            return storage.load_memory() or {}
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        logger.error(f"Curiosity could not read long-term memory: {e}")
    return {}


def known_keys(memory=None) -> set:
    return set((_memory() if memory is None else memory).keys())


def candidates(memory=None) -> list:
    """The seeded questions still worth asking, in order.

    Order is the order they are written in: the territory first, then the
    people, then the preferences. It is a deliberate curriculum rather than a
    shuffle - the things that make the assistant wrong most often come first.
    """
    known = known_keys(memory)
    return [q for q in SEEDS if q.key not in known]


def generated(memory=None):
    """One question from the model, for when the seeds are exhausted.

    Returns None on anything unexpected, and None is a perfectly good answer:
    it means the assistant asks nothing today. Gemini is skipped for the same
    reason it is skipped in triage - its free tier is twenty calls a day and
    those belong to questions Itai actually asked.
    """
    memory = _memory() if memory is None else memory
    known = set(memory.keys())
    facts = json.dumps(memory, ensure_ascii=False)[:4000]
    answer = llm.ask_json(
        f"Everything the assistant knows about him:\n{facts}\n\nPropose one question.",
        system=_SYSTEM,
        max_tokens=200,
        skip=("gemini",),
    )
    if not isinstance(answer, dict):
        return None

    key = str(answer.get("key") or "").strip().lower()
    text = str(answer.get("question") or "").strip()
    category = str(answer.get("category") or "general").strip() or "general"
    if not _KEY.match(key) or key in known:
        logger.info("Curiosity discarded a generated key: %r", key)
        return None
    if not 8 <= len(text) <= 300:
        logger.info("Curiosity discarded a generated question of length %d", len(text))
        return None
    return Question(key, text, category[:40])


def ask_next(claim) -> object:
    """The whole decision: the next question that has not been asked yet.

    claim(key) is passed in rather than called directly, so this module never
    needs to know what the proactive log is. It returns True when this run is
    the one that gets to ask that question, and False when someone already has
    - a question he never answered stays claimed forever, which is exactly the
    "asked once, ever" rule.
    """
    memory = _memory()
    for question in candidates(memory):
        if claim(question.key):
            return question

    # Only once every seed is either answered or already asked. Costs a model
    # call a day at that point, on a free tier, and stops if there is no key.
    question = generated(memory)
    if question and claim(question.key):
        return question
    return None
