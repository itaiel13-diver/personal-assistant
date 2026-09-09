"""Microsoft To Do, as a set of tools the assistant can actually use.

The point of connecting this rather than leaning on the reminder table we
already have: To Do is where Itai's tasks live when he is not talking to the
assistant. A reminder here is one he sees on his phone's task list, in the same
place as everything he wrote there himself, and it survives this assistant
entirely. `create_reminder` interrupts him at a moment; a To Do task waits for
him. They are different jobs and both are worth having.

Scope of what is exposed, deliberately: everything. Lists and tasks can be
created, read, changed, completed, reopened and deleted, because Itai's
standing instruction is to hand over the whole capability at once and narrow it
later rather than discover a missing verb mid-task. There is no equivalent here
of the never-send guard in gmail_tools or the never-share guard in drive_tools,
and that is a decision rather than an oversight: a To Do list is private to him,
nothing in it can be sent to another person, and the worst case is a task he has
to retype. Deleting is still the last resort - completing a task keeps it,
deleting it does not - and the tools say so where it matters.

Naming: every tool takes a list or task by NAME, not by an opaque Graph id,
because the model is working from what Itai said out loud ("תוסיף לרשימת קניות")
and has no id to work from. Resolution is exact-match first, then unique
substring; an ambiguous name is reported back rather than guessed at, since
guessing writes into the wrong list silently.
"""

import logging

import msgraph
import reminders

logger = logging.getLogger(__name__)

# The timezone every date sent to Graph is stamped with. Without it Graph reads
# a naive datetime as UTC, which puts a 9am Israel task on the previous day for
# anything before 03:00 and makes "tomorrow" mean the wrong tomorrow.
TIMEZONE = "Asia/Jerusalem"

MAX_TASKS_SHOWN = 40

IMPORTANCE = ("low", "normal", "high")

_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

NOT_CONNECTED = (
    "❌ החיבור ל-Microsoft To Do עדיין לא הוגדר. צריך MS_CLIENT_ID, MS_CLIENT_SECRET "
    "ו-MS_REFRESH_TOKEN. תגיד לאיתי שצריך לאשר את החיבור בדפדפן פעם אחת."
)


def _fail(action: str, error: Exception) -> str:
    """One shape for every failure, so the model reports it instead of inventing
    a success. The status matters: 401/403 means the consent is wrong or gone,
    and re-approving is the fix - retrying is not."""
    logger.error(f"Microsoft To Do — {action} failed: {error}")
    status = getattr(error, "status", 0)
    if status in (401, 403):
        return (
            f"❌ Microsoft דחתה את הבקשה ({status}). כנראה צריך לאשר מחדש את החיבור "
            f"בדפדפן. אל תבטיח שהמשימה נשמרה."
        )
    return f"❌ לא הצלחתי {action}: {error}"


# --- resolution ---------------------------------------------------------


def _lists() -> list:
    return msgraph.get_all("/me/todo/lists")


def _match(items: list, needle: str, field: str) -> list:
    needle = (needle or "").strip().casefold()
    exact = [i for i in items if (i.get(field) or "").strip().casefold() == needle]
    if exact:
        return exact
    return [i for i in items if needle in (i.get(field) or "").casefold()]


def _default_list(items: list) -> dict | None:
    for item in items:
        if item.get("wellknownListName") == "defaultList":
            return item
    return items[0] if items else None


def _resolve_list(name: str) -> dict:
    """The list a tool should act on. An empty name means his default list -
    the one To Do calls "Tasks" and shows first - which is where a task belongs
    when he did not say otherwise."""
    items = _lists()
    if not items:
        raise LookupError("אין אף רשימה בחשבון ה-Microsoft To Do הזה.")
    if not (name or "").strip():
        return _default_list(items)
    found = _match(items, name, "displayName")
    if not found:
        names = ", ".join((i.get("displayName") or "?") for i in items)
        raise LookupError(f'לא מצאתי רשימה בשם "{name}". הרשימות הקיימות: {names}')
    if len(found) > 1:
        names = ", ".join((i.get("displayName") or "?") for i in found)
        raise LookupError(f'"{name}" מתאים ליותר מרשימה אחת: {names}. תשאל אותו לאיזו.')
    return found[0]


def _tasks(list_id: str, include_completed: bool = False) -> list:
    params = None if include_completed else {"$filter": "status ne 'completed'"}
    return msgraph.get_all(f"/me/todo/lists/{list_id}/tasks", params=params)


def _resolve_task(list_id: str, needle: str) -> dict:
    """Completed tasks are searched too, so "reopen the thing I ticked by
    mistake" can find it - the alternative is a task that has become invisible
    to the assistant the moment it was completed."""
    items = _tasks(list_id, include_completed=True)
    found = _match(items, needle, "title")
    if not found:
        raise LookupError(f'לא מצאתי משימה בשם "{needle}" ברשימה הזאת.')
    if len(found) > 1:
        titles = ", ".join((i.get("title") or "?") for i in found[:5])
        raise LookupError(f'"{needle}" מתאים ליותר ממשימה אחת: {titles}. תשאל אותו לאיזו.')
    return found[0]


# --- building what Graph wants ------------------------------------------


def _stamp(when: str):
    """A Hebrew or ISO time phrase turned into a Graph dateTimeTimeZone.

    Reuses the reminder parser rather than growing a second one, so "מחר בבוקר"
    means the same thing whether it becomes a WhatsApp reminder or a To Do task.
    """
    parsed = reminders.parse_when(when)
    if parsed is None:
        return None, None
    return {"dateTime": parsed.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE}, parsed


def _recurrence(repeat: str, start) -> dict | None:
    """Graph's patternedRecurrence for the repeats worth supporting.

    Graph rejects a recurrence with no due date, so the caller must have one
    before calling this - which is why `start` is required rather than optional.
    """
    kind = reminders.normalise_recurrence(repeat)
    if kind == "once":
        return None
    weekday = _WEEKDAY_NAMES[start.weekday()]
    patterns = {
        "daily": {"type": "daily", "interval": 1},
        "weekly": {"type": "weekly", "interval": 1, "daysOfWeek": [weekday]},
        # Israel's working week, which is what "בימי עבודה" means here and is
        # not what Graph would assume from a locale.
        "weekdays": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["sunday", "monday", "tuesday", "wednesday", "thursday"],
        },
        "monthly": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": start.day},
    }
    pattern = patterns.get(kind)
    if pattern is None:
        return None
    return {
        "pattern": pattern,
        "range": {"type": "noEnd", "startDate": start.strftime("%Y-%m-%d")},
    }


def _describe(task: dict) -> str:
    """One task as a line Itai can read, with only the parts that are set."""
    bits = []
    if task.get("status") == "completed":
        bits.append("✔")
    bits.append(task.get("title") or "(ללא כותרת)")
    due = (task.get("dueDateTime") or {}).get("dateTime")
    if due:
        bits.append(f"— עד {due[8:10]}/{due[5:7]}")
    if task.get("importance") == "high":
        bits.append("‼")
    if task.get("recurrence"):
        bits.append("(חוזרת)")
    checklist = task.get("checklistItems") or []
    if checklist:
        done = sum(1 for c in checklist if c.get("isChecked"))
        bits.append(f"[{done}/{len(checklist)}]")
    return " ".join(bits)


# --- the tools ----------------------------------------------------------


def todo_connection_status() -> str:
    """Checks whether Microsoft To Do is connected, and to which account.

    Use this when Itai asks if the To Do connection works, or when a To Do call
    failed and you need to say why.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        who = msgraph.account()
        lists = _lists()
        return (
            f"✅ מחובר ל-Microsoft To Do כ-{who}. "
            f"יש {len(lists)} רשימות: " + ", ".join((i.get("displayName") or "?") for i in lists)
        )
    except Exception as e:
        return _fail("לבדוק את החיבור", e)


def list_todo_lists() -> str:
    """Lists the names of Itai's Microsoft To Do task lists, his own and shared ones."""
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        items = _lists()
        if not items:
            return "אין אף רשימה ב-Microsoft To Do."
        lines = []
        for item in items:
            name = item.get("displayName") or "?"
            marks = []
            if item.get("wellknownListName") == "defaultList":
                marks.append("ברירת מחדל")
            if item.get("isShared"):
                marks.append("משותפת")
            lines.append(f"• {name}" + (f" ({', '.join(marks)})" if marks else ""))
        return "\n".join(lines)
    except Exception as e:
        return _fail("לקרוא את הרשימות", e)


def list_todo_tasks(list_name: str = "", include_completed: bool = False) -> str:
    """Reads the open tasks in one of Itai's Microsoft To Do lists.

    Args:
        list_name: Which list. Leave empty for his default list ("Tasks").
        include_completed: True to show tasks he has already ticked off too.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(list_name)
        items = _tasks(target["id"], include_completed)
        if not items:
            return f'הרשימה "{target.get("displayName")}" ריקה.'
        shown = items[:MAX_TASKS_SHOWN]
        head = f'📋 {target.get("displayName")} ({len(items)} משימות):'
        tail = "" if len(items) == len(shown) else f"\n… ועוד {len(items) - len(shown)}."
        return head + "\n" + "\n".join(f"• {_describe(t)}" for t in shown) + tail
    except Exception as e:
        return _fail("לקרוא את המשימות", e)


def search_todo_tasks(query: str, include_completed: bool = False) -> str:
    """Searches every Microsoft To Do list for tasks whose title matches.

    Use this when Itai names a task but not the list it is on.

    Args:
        query: Words from the task title.
        include_completed: True to search tasks he has already completed too.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        hits = []
        for item in _lists():
            for task in _match(_tasks(item["id"], include_completed), query, "title"):
                hits.append(f'• {_describe(task)}  ← {item.get("displayName")}')
        if not hits:
            return f'לא מצאתי משימה שמתאימה ל"{query}".'
        return "\n".join(hits[:MAX_TASKS_SHOWN])
    except Exception as e:
        return _fail("לחפש משימות", e)


def create_todo_task(
    title: str,
    list_name: str = "",
    due: str = "",
    reminder: str = "",
    notes: str = "",
    importance: str = "normal",
    repeat: str = "once",
) -> str:
    """Adds a task to Microsoft To Do, where Itai will see it on his phone.

    Prefer this over create_reminder for anything he has to DO. Use
    create_reminder when the point is to interrupt him at a specific moment.

    Args:
        title: The task itself, in Hebrew, phrased as the task ("לשלוח דוח לדנה").
        list_name: Which list. Leave empty for his default list.
        due: When it is due. An ISO local time ("2026-09-10T09:00") or a Hebrew
            phrase ("מחר", "ביום ראשון"). Leave empty for no due date.
        reminder: When To Do should pop a reminder for it, same formats. Leave
            empty for none.
        notes: Extra detail to put in the task's body.
        importance: "low", "normal" or "high".
        repeat: "once", "daily", "weekdays" (Sunday-Thursday), "weekly" or
            "monthly". Anything other than "once" needs a due date; if he did
            not give one, today is used.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    if not (title or "").strip():
        return "❌ אין כותרת למשימה."
    try:
        target = _resolve_list(list_name)
        # A batch that died mid-loop gets retried as a whole; without this
        # check the tasks that DID get created would be duplicated.
        wanted = title.strip().lower()
        for task in _tasks(target["id"], False):
            if (task.get("title") or "").strip().lower() == wanted:
                return f'ℹ️ כבר קיימת משימה פתוחה בשם "{title.strip()}" - לא נוצרה כפילות.'
        body = {"title": title.strip()}

        if (importance or "").strip() in IMPORTANCE:
            body["importance"] = importance.strip()
        if (notes or "").strip():
            body["body"] = {"content": notes.strip(), "contentType": "text"}

        due_stamp, due_at = _stamp(due) if (due or "").strip() else (None, None)
        recurrence = _recurrence(repeat, due_at) if due_at else None
        if recurrence is None and reminders.normalise_recurrence(repeat) != "once":
            # Graph refuses a recurrence without a due date, so rather than
            # dropping the repeat he asked for, anchor it to today and say so.
            from datetime import datetime
            due_at = datetime.now(reminders.ISRAEL_TZ)
            due_stamp = {"dateTime": due_at.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE}
            recurrence = _recurrence(repeat, due_at)
        if due_stamp:
            body["dueDateTime"] = due_stamp
        if recurrence:
            body["recurrence"] = recurrence

        if (reminder or "").strip():
            stamp, _ = _stamp(reminder)
            if stamp:
                body["reminderDateTime"] = stamp
                body["isReminderOn"] = True

        created = msgraph.graph("POST", f"/me/todo/lists/{target['id']}/tasks", json_body=body)
        return f'✅ נוספה ל-"{target.get("displayName")}": {_describe(created or body)}'
    except Exception as e:
        return _fail("להוסיף את המשימה", e)


def update_todo_task(
    task: str,
    list_name: str = "",
    new_title: str = "",
    due: str = "",
    reminder: str = "",
    notes: str = "",
    importance: str = "",
) -> str:
    """Changes an existing Microsoft To Do task. Only the fields you pass change.

    Args:
        task: Words from the current task title.
        list_name: Which list it is on. Leave empty for his default list.
        new_title: A new title, if he wants it renamed.
        due: A new due date, ISO or Hebrew. Pass "none" to clear it.
        reminder: A new reminder time. Pass "none" to turn the reminder off.
        notes: Replacement body text.
        importance: "low", "normal" or "high".
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(list_name)
        existing = _resolve_task(target["id"], task)
        body = {}

        if (new_title or "").strip():
            body["title"] = new_title.strip()
        if (notes or "").strip():
            body["body"] = {"content": notes.strip(), "contentType": "text"}
        if (importance or "").strip() in IMPORTANCE:
            body["importance"] = importance.strip()

        for field, value, extra in (
            ("dueDateTime", due, {}),
            ("reminderDateTime", reminder, {"isReminderOn": True}),
        ):
            value = (value or "").strip()
            if not value:
                continue
            if value.casefold() in ("none", "אין", "בטל", "ללא"):
                body[field] = None
                if field == "reminderDateTime":
                    body["isReminderOn"] = False
                continue
            stamp, _ = _stamp(value)
            if stamp is None:
                return f'❌ לא הבנתי את התאריך "{value}". תשאל אותו מתי בדיוק.'
            body[field] = stamp
            body.update(extra)

        if not body:
            return "❌ לא ביקשת לשנות שום דבר במשימה."
        updated = msgraph.graph(
            "PATCH", f"/me/todo/lists/{target['id']}/tasks/{existing['id']}", json_body=body
        )
        return f"✅ עודכנה: {_describe(updated or {**existing, **body})}"
    except Exception as e:
        return _fail("לעדכן את המשימה", e)


def complete_todo_task(task: str, list_name: str = "") -> str:
    """Marks a Microsoft To Do task as done.

    Args:
        task: Words from the task title.
        list_name: Which list it is on. Leave empty for his default list.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(list_name)
        existing = _resolve_task(target["id"], task)
        msgraph.graph(
            "PATCH",
            f"/me/todo/lists/{target['id']}/tasks/{existing['id']}",
            json_body={"status": "completed"},
        )
        return f'✅ סומנה כבוצעה: {existing.get("title")}'
    except Exception as e:
        return _fail("לסמן את המשימה כבוצעה", e)


def reopen_todo_task(task: str, list_name: str = "") -> str:
    """Un-completes a Microsoft To Do task he ticked off, putting it back on the list.

    Args:
        task: Words from the task title.
        list_name: Which list it is on. Leave empty for his default list.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(list_name)
        existing = _resolve_task(target["id"], task)
        msgraph.graph(
            "PATCH",
            f"/me/todo/lists/{target['id']}/tasks/{existing['id']}",
            json_body={"status": "notStarted"},
        )
        return f'✅ הוחזרה לרשימה: {existing.get("title")}'
    except Exception as e:
        return _fail("להחזיר את המשימה לרשימה", e)


def delete_todo_task(task: str, list_name: str = "") -> str:
    """Deletes a Microsoft To Do task permanently. To Do has no bin for tasks.

    Prefer complete_todo_task - a completed task can be brought back, a deleted
    one cannot. Only delete when he actually says to delete.

    Args:
        task: Words from the task title.
        list_name: Which list it is on. Leave empty for his default list.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(list_name)
        existing = _resolve_task(target["id"], task)
        msgraph.graph("DELETE", f"/me/todo/lists/{target['id']}/tasks/{existing['id']}")
        return f'🗑️ נמחקה לצמיתות: {existing.get("title")}'
    except Exception as e:
        return _fail("למחוק את המשימה", e)


def add_todo_checklist_item(task: str, item: str, list_name: str = "") -> str:
    """Adds a sub-step to an existing Microsoft To Do task.

    Args:
        task: Words from the task title.
        item: The sub-step to add, in Hebrew.
        list_name: Which list the task is on. Leave empty for his default list.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    if not (item or "").strip():
        return "❌ אין תוכן לתת-משימה."
    try:
        target = _resolve_list(list_name)
        existing = _resolve_task(target["id"], task)
        msgraph.graph(
            "POST",
            f"/me/todo/lists/{target['id']}/tasks/{existing['id']}/checklistItems",
            json_body={"displayName": item.strip()},
        )
        return f'✅ נוספה תת-משימה ל"{existing.get("title")}": {item.strip()}'
    except Exception as e:
        return _fail("להוסיף תת-משימה", e)


def create_todo_list(name: str) -> str:
    """Creates a new task list in Microsoft To Do.

    Args:
        name: The list's name, in Hebrew.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    if not (name or "").strip():
        return "❌ אין שם לרשימה."
    try:
        created = msgraph.graph("POST", "/me/todo/lists", json_body={"displayName": name.strip()})
        return f'✅ נוצרה רשימה: {created.get("displayName", name.strip())}'
    except Exception as e:
        return _fail("ליצור רשימה", e)


def delete_todo_list(name: str) -> str:
    """Deletes a whole Microsoft To Do list and every task on it, permanently.

    This is the most destructive thing you can do here. Only ever call it when
    Itai has named the list and said to delete it. Never as tidying up.

    Args:
        name: The exact list name.
    """
    if not msgraph.configured():
        return NOT_CONNECTED
    try:
        target = _resolve_list(name)
        if not (name or "").strip():
            return "❌ לא אמחק רשימה בלי שתגיד לי בדיוק איזו."
        count = len(_tasks(target["id"], include_completed=True))
        msgraph.graph("DELETE", f"/me/todo/lists/{target['id']}")
        return f'🗑️ הרשימה "{target.get("displayName")}" נמחקה על {count} המשימות שבה.'
    except Exception as e:
        return _fail("למחוק את הרשימה", e)
