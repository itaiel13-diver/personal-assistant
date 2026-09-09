"""Carries the assistant's tools over to the OpenAI-compatible fallback tier.

Gemini's SDK runs function calling for free: declare the Python functions and
it executes whatever the model asks for. The fallback providers in llm.py
speak the OpenAI protocol, where the same feature needs three pieces this
module supplies: JSON schemas for the tools, a dispatcher that executes the
call the model asks for, and a way to choose WHICH tools to offer.

Why choose at all. Every schema travels inside the request, and all 39 of
them together are roughly 6,000 tokens - nearly the whole 8,000
tokens-per-minute budget of Groq's free tier before a word of the
conversation. So the fallback offers the one or two packs the message is
actually about (a To Do question carries the To Do pack) plus the tiny core
pack, and a message about nothing in particular gets the old tool-less
answer. The packs are keyed by name so they mirror assistant.tools_list one
for one; the functions themselves are passed in from there, so the guards
inside them - working-tree restrictions, shared-file confirmations,
disambiguation refusals - hold exactly as they do on the Gemini path.
"""

import inspect
import json
import logging

logger = logging.getLogger(__name__)

# The core pack rides along on every tool-enabled fallback call: saving a
# fact to long-term memory costs 20 tokens of schema and is the difference
# between a fallback that forgets and one that learns.
CORE_PACK = ("save_to_long_term_memory",)

# Pack names -> keywords that pull the pack into the request. Hebrew first -
# that is what Itai actually writes - with the English for loanwords he uses.
PACKS = {
    "todo": (
        "משימ", "טודו", "to do", "todo", "צ'קליסט", "צקליסט",
        "לסמן", "רשימה", "הוסף לרשימה", "תוסיף משימה",
    ),
    "calendar": (
        "פגיש", "יומן", "אירוע", "מפגש", "לזמן", "שיבוץ", "calendar",
        "פגישה", "זימון",
    ),
    "mail": (
        "מייל", "דואר", "gmail", "e-mail", "email", "מכתב", "טיוט",
    ),
    "drive": (
        "קובץ", "קבצים", "תיקי", "דרייב", "drive", "מסמך", "גוגל דוקס",
        "שיתוף", "לשתף",
    ),
    "web": (
        "חפש", "חיפוש", "גוגל", "search", "כמה עולה", "מחיר של", "חדשות",
        "תוצאות", "משחק", "ליגה", "ויקיפדיה", "אינטרנט",
    ),
    "reminders": (
        "תזכורת", "תזכיר", "remind", "מספר חוג",  # מספר חוג = recurring nudge
    ),
}

PACK_MEMBERS = {
    "todo": (
        "list_todo_lists", "list_todo_tasks", "search_todo_tasks",
        "create_todo_task", "update_todo_task", "complete_todo_task",
        "reopen_todo_task", "delete_todo_task", "add_todo_checklist_item",
        "create_todo_list", "delete_todo_list", "todo_connection_status",
    ),
    "calendar": (
        "get_calendar_events", "create_calendar_event",
        "update_calendar_event", "delete_calendar_event",
    ),
    "mail": (
        "search_emails", "read_email", "read_email_attachment",
        "search_email_attachment", "create_email_draft",
    ),
    "drive": (
        "search_drive", "list_drive_folder", "read_drive_file",
        "save_to_drive_folder", "list_bot_shares", "create_drive_file",
        "create_drive_folder", "append_drive_file", "update_drive_file",
        "rename_drive_file", "move_drive_file", "trash_drive_file",
    ),
    "web": ("search_web", "read_web_page"),
    "reminders": ("create_reminder", "list_reminders", "cancel_reminder"),
}

_JSON_TYPES = {
    str: ("string", None),
    int: ("integer", None),
    float: ("number", None),
    bool: ("boolean", None),
    list: ("array", {"items": {"type": "string"}}),
}


def _annotation_schema(annotation) -> dict:
    """One Python type hint -> one JSON schema fragment. Anything exotic
    degrades to a plain string, which is how the model would have passed it
    anyway."""
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:  # list[str], Optional[...] and friends
        args = [a for a in getattr(annotation, "__args__", ()) if a is not type(None)]
        if origin is list and args:
            return {"type": "array", "items": {"type": _JSON_TYPES.get(args[0], ("string", None))[0]}}
        if args:
            annotation = args[0]
    type_name, extra = _JSON_TYPES.get(annotation, ("string", None))
    schema = {"type": type_name}
    if extra:
        schema.update(extra)
    return schema


def schema_for(fn) -> dict:
    """One Python function -> one OpenAI tool schema. The docstring goes in
    whole: the confirmation rules live in it, and the fallback model must
    follow them exactly the way Gemini does."""
    signature = inspect.signature(fn)
    properties = {}
    required = []
    for name, param in signature.parameters.items():
        if param.annotation is inspect.Parameter.empty:
            schema = {"type": "string"}
        else:
            schema = _annotation_schema(param.annotation)
        properties[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": inspect.getdoc(fn) or fn.__name__,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_registry(tools: list) -> dict:
    """assistant.tools_list as a name -> function map."""
    return {fn.__name__: fn for fn in tools}


def select_packs(text: str) -> list:
    """The packs a message is about, by keyword. No match means the old
    tool-less answer - a conversation about nothing actionable should not
    pay for schemas it will never call."""
    haystack = (text or "").lower()
    chosen = [name for name, keywords in PACKS.items()
              if any(keyword.lower() in haystack for keyword in keywords)]
    return chosen


def tools_for(pack_names: list, registry: dict) -> list:
    """Schemas for the chosen packs plus the core pack, in a stable order."""
    names = list(CORE_PACK)
    for pack in pack_names:
        names.extend(PACK_MEMBERS.get(pack, ()))
    schemas = []
    for name in names:
        fn = registry.get(name)
        if fn is None:
            logger.warning(f"tool_bridge: {name} is not in the registry - skipped")
            continue
        schemas.append(schema_for(fn))
    return schemas


def dispatch(registry: dict, name: str, arguments_json: str) -> str:
    """Executes one tool call the model asked for, and always answers with a
    string - the loop feeds whatever happens back to the model, because a
    raised exception that kills the webhook is worse than a tool that says
    what went wrong."""
    fn = registry.get(name)
    if fn is None:
        logger.warning(f"tool_bridge: model asked for an unknown tool '{name}'")
        return f"❌ הכלי '{name}' לא זמין כרגע."
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        logger.warning(f"tool_bridge: bad arguments for {name}: {arguments_json[:120]!r}")
        return f"❌ הארגומנטים ל-{name} לא התפרסרו."
    try:
        result = fn(**arguments)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except TypeError as e:
        logger.warning(f"tool_bridge: {name} rejected the arguments: {e}")
        return f"❌ הארגומנטים לא התאימו ל-{name}: {e}"
    except Exception as e:
        logger.error(f"tool_bridge: {name} raised: {e}")
        return f"❌ הפעולה {name} נכשלה: {e}"
