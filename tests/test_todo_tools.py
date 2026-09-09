"""The To Do tools, exercised against a fake Graph.

The interesting behaviour here is not the HTTP - msgraph's tests cover that -
it is name resolution. The model works from what Itai said out loud, so every
tool takes a name, and the two ways that goes wrong are picking the wrong list
silently and refusing a name that was perfectly clear.
"""

import ast

import pytest

import msgraph
import todo_tools

LISTS = [
    {"id": "L1", "displayName": "משימות", "wellknownListName": "defaultList"},
    {"id": "L2", "displayName": "קניות"},
    {"id": "L3", "displayName": "קניות לבית", "isShared": True},
]

TASKS = {
    "L1": [
        {"id": "T1", "title": "לשלוח דוח לדנה", "status": "notStarted"},
        {"id": "T2", "title": "להזמין רכב לטיפול", "status": "completed"},
    ],
    "L2": [{"id": "T3", "title": "חלב", "status": "notStarted"}],
    "L3": [],
}


class Graph:
    """A stand-in Graph that records writes and answers reads from the fixtures."""

    def __init__(self):
        self.writes = []

    def get_all(self, path, params=None, max_pages=10):
        if path == "/me/todo/lists":
            return list(LISTS)
        list_id = path.split("/")[4]
        tasks = list(TASKS.get(list_id, []))
        if params and "status ne 'completed'" in str(params):
            tasks = [t for t in tasks if t.get("status") != "completed"]
        return tasks

    def graph(self, method, path, json_body=None, params=None):
        self.writes.append((method, path, json_body))
        if method == "POST" and path.endswith("/tasks"):
            return {"id": "NEW", **(json_body or {})}
        if method == "POST" and path == "/me/todo/lists":
            return {"id": "NEW", **(json_body or {})}
        if path == "/me":
            return {"userPrincipalName": "itai@example.com"}
        return {}


@pytest.fixture
def api(monkeypatch):
    fake = Graph()
    monkeypatch.setattr(msgraph, "configured", lambda: True)
    monkeypatch.setattr(msgraph, "get_all", fake.get_all)
    monkeypatch.setattr(msgraph, "graph", fake.graph)
    monkeypatch.setattr(msgraph, "account", lambda: "itai@example.com")
    return fake


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(msgraph, "configured", lambda: False)
    monkeypatch.setattr(msgraph, "get_all", lambda *a, **k: pytest.fail("must not call out"))
    monkeypatch.setattr(msgraph, "graph", lambda *a, **k: pytest.fail("must not call out"))


def body(fake, index=0):
    return fake.writes[index][2]


# --- an unconfigured connection ----------------------------------------


EVERY_TOOL = [
    (todo_tools.list_todo_lists, ()),
    (todo_tools.list_todo_tasks, ()),
    (todo_tools.search_todo_tasks, ("חלב",)),
    (todo_tools.create_todo_task, ("משימה",)),
    (todo_tools.update_todo_task, ("משימה",)),
    (todo_tools.complete_todo_task, ("משימה",)),
    (todo_tools.reopen_todo_task, ("משימה",)),
    (todo_tools.delete_todo_task, ("משימה",)),
    (todo_tools.add_todo_checklist_item, ("משימה", "צעד")),
    (todo_tools.create_todo_list, ("רשימה",)),
    (todo_tools.delete_todo_list, ("רשימה",)),
    (todo_tools.todo_connection_status, ()),
]


@pytest.mark.parametrize("tool,args", EVERY_TOOL, ids=lambda v: getattr(v, "__name__", ""))
def test_no_tool_reaches_out_before_the_connection_exists(tool, args, unconfigured):
    """Every one of them, because the one that forgets is the one that raises
    inside a tool call and gets reported to Itai as a fault in his phone."""
    assert tool(*args) == todo_tools.NOT_CONNECTED


# --- resolving a list --------------------------------------------------


def test_no_list_name_means_his_default_list(api):
    assert "משימות" in todo_tools.list_todo_tasks()


def test_an_exact_name_beats_a_longer_one_that_contains_it(api):
    """"קניות" is also a prefix of "קניות לבית". Without exact-match-first this
    is ambiguous and he gets a question instead of his shopping list."""
    out = todo_tools.list_todo_tasks("קניות")
    assert "חלב" in out


def test_a_partial_name_that_matches_one_list_is_accepted(api):
    assert "ריקה" in todo_tools.list_todo_tasks("לבית")


def test_an_unknown_list_says_which_lists_exist(api):
    out = todo_tools.list_todo_tasks("אין כזאת")
    assert "❌" in out and "משימות" in out


def test_an_ambiguous_task_name_asks_rather_than_guesses(api):
    """Guessing here writes into the wrong place silently, which is the one
    outcome worse than asking."""
    out = todo_tools.complete_todo_task("קני", "קניות")
    assert "❌" in out
    assert api.writes == []


def test_shared_lists_are_shown_as_shared(api):
    assert "משותפת" in todo_tools.list_todo_lists()


def test_the_default_list_is_marked(api):
    assert "ברירת מחדל" in todo_tools.list_todo_lists()


# --- reading -----------------------------------------------------------


def test_completed_tasks_are_hidden_by_default(api):
    assert "להזמין רכב" not in todo_tools.list_todo_tasks()


def test_completed_tasks_can_be_asked_for(api):
    assert "להזמין רכב" in todo_tools.list_todo_tasks("", include_completed=True)


def test_search_spans_every_list_and_names_the_one_it_found(api):
    out = todo_tools.search_todo_tasks("חלב")
    assert "חלב" in out and "קניות" in out


def test_search_that_finds_nothing_says_so(api):
    assert "לא מצאתי" in todo_tools.search_todo_tasks("אבטיח")


def test_an_empty_list_reads_as_empty_not_as_an_error(api):
    assert "ריקה" in todo_tools.list_todo_tasks("קניות לבית")


# --- creating ----------------------------------------------------------


def test_a_task_is_created_on_the_named_list(api):
    out = todo_tools.create_todo_task("חלב שוקו", "קניות")
    assert "✅" in out
    method, path, sent = api.writes[0]
    assert method == "POST" and path == "/me/todo/lists/L2/tasks"
    assert sent["title"] == "חלב שוקו"


def test_a_task_with_no_list_goes_to_the_default_list(api):
    todo_tools.create_todo_task("משהו")
    assert api.writes[0][1] == "/me/todo/lists/L1/tasks"


def test_a_due_date_is_stamped_with_israel_time(api):
    todo_tools.create_todo_task("משהו", due="2026-09-10T09:00")
    due = body(api)["dueDateTime"]
    assert due["timeZone"] == "Asia/Jerusalem"
    assert due["dateTime"].startswith("2026-09-10T09:00")


def test_a_hebrew_time_phrase_is_understood(api):
    todo_tools.create_todo_task("משהו", due="מחר בבוקר")
    assert "dueDateTime" in body(api)


def test_a_reminder_turns_the_reminder_flag_on(api):
    todo_tools.create_todo_task("משהו", reminder="2026-09-10T09:00")
    assert body(api)["isReminderOn"] is True
    assert "reminderDateTime" in body(api)


def test_notes_become_the_task_body(api):
    todo_tools.create_todo_task("משהו", notes="לבדוק מול ניקיטה")
    assert body(api)["body"] == {"content": "לבדוק מול ניקיטה", "contentType": "text"}


def test_importance_is_passed_through(api):
    todo_tools.create_todo_task("משהו", importance="high")
    assert body(api)["importance"] == "high"


def test_a_nonsense_importance_is_dropped_rather_than_sent(api):
    todo_tools.create_todo_task("משהו", importance="דחוף מאוד")
    assert "importance" not in body(api)


def test_a_repeat_becomes_a_graph_recurrence(api):
    todo_tools.create_todo_task("משהו", due="2026-09-10T09:00", repeat="daily")
    assert body(api)["recurrence"]["pattern"]["type"] == "daily"


def test_weekdays_means_the_israeli_working_week(api):
    todo_tools.create_todo_task("משהו", due="2026-09-10T09:00", repeat="weekdays")
    days = body(api)["recurrence"]["pattern"]["daysOfWeek"]
    assert days == ["sunday", "monday", "tuesday", "wednesday", "thursday"]
    assert "friday" not in days


def test_a_repeat_without_a_due_date_is_anchored_rather_than_dropped(api):
    """Graph refuses a recurrence with no due date. Silently dropping the
    repeat he asked for is worse than starting it today."""
    todo_tools.create_todo_task("משהו", repeat="weekly")
    sent = body(api)
    assert "recurrence" in sent
    assert "dueDateTime" in sent


def test_a_one_off_task_carries_no_recurrence(api):
    todo_tools.create_todo_task("משהו", due="2026-09-10T09:00")
    assert "recurrence" not in body(api)


def test_an_empty_title_is_refused_without_calling_out(api):
    assert "❌" in todo_tools.create_todo_task("   ")
    assert api.writes == []


# --- updating ----------------------------------------------------------


def test_only_the_fields_given_are_sent(api):
    todo_tools.update_todo_task("דוח לדנה", new_title="לשלוח דוח לדנה ולניקיטה")
    method, path, sent = api.writes[0]
    assert method == "PATCH" and path == "/me/todo/lists/L1/tasks/T1"
    assert sent == {"title": "לשלוח דוח לדנה ולניקיטה"}


def test_a_due_date_can_be_cleared(api):
    todo_tools.update_todo_task("דוח לדנה", due="none")
    assert body(api)["dueDateTime"] is None


def test_clearing_a_reminder_also_turns_the_flag_off(api):
    todo_tools.update_todo_task("דוח לדנה", reminder="אין")
    assert body(api)["reminderDateTime"] is None
    assert body(api)["isReminderOn"] is False


def test_an_unparseable_date_asks_rather_than_guessing(api):
    out = todo_tools.update_todo_task("דוח לדנה", due="מתישהו")
    assert "❌" in out
    assert api.writes == []


def test_an_update_that_changes_nothing_says_so(api):
    assert "❌" in todo_tools.update_todo_task("דוח לדנה")
    assert api.writes == []


# --- finishing and removing --------------------------------------------


def test_completing_sets_the_status(api):
    out = todo_tools.complete_todo_task("דוח לדנה")
    assert "✅" in out
    assert body(api) == {"status": "completed"}


def test_a_completed_task_can_still_be_found_to_reopen(api):
    """A task that vanishes from the assistant's view the moment it is ticked
    cannot be un-ticked, which is the whole point of reopen."""
    out = todo_tools.reopen_todo_task("רכב לטיפול")
    assert "✅" in out
    assert body(api) == {"status": "notStarted"}


def test_deleting_a_task_calls_delete_on_it(api):
    out = todo_tools.delete_todo_task("דוח לדנה")
    assert "🗑️" in out
    assert api.writes[0][0] == "DELETE"
    assert api.writes[0][1] == "/me/todo/lists/L1/tasks/T1"


def test_deleting_a_list_names_it_and_says_how_much_went_with_it(api):
    out = todo_tools.delete_todo_list("קניות")
    assert "קניות" in out and "1" in out
    assert ("DELETE", "/me/todo/lists/L2", None) in api.writes


def test_a_list_is_never_deleted_without_being_named(api):
    """An empty name resolves to his default list, which is the one list that
    must never be deleted by an assistant filling in a blank."""
    out = todo_tools.delete_todo_list("")
    assert "❌" in out
    assert api.writes == []


# --- checklists and lists ----------------------------------------------


def test_a_checklist_item_is_posted_under_its_task(api):
    out = todo_tools.add_todo_checklist_item("דוח לדנה", "לבדוק מספרים")
    assert "✅" in out
    assert api.writes[0][1] == "/me/todo/lists/L1/tasks/T1/checklistItems"
    assert body(api) == {"displayName": "לבדוק מספרים"}


def test_an_empty_checklist_item_is_refused(api):
    assert "❌" in todo_tools.add_todo_checklist_item("דוח לדנה", "  ")
    assert api.writes == []


def test_creating_a_list(api):
    out = todo_tools.create_todo_list("נסיעות")
    assert "✅" in out
    assert api.writes[0] == ("POST", "/me/todo/lists", {"displayName": "נסיעות"})


def test_an_empty_list_name_is_refused(api):
    assert "❌" in todo_tools.create_todo_list("")
    assert api.writes == []


# --- failure ------------------------------------------------------------


def test_a_rejected_call_never_reads_as_a_success(monkeypatch, api):
    def refuse(*a, **k):
        raise msgraph.GraphError(500, "boom")

    monkeypatch.setattr(msgraph, "graph", refuse)
    out = todo_tools.create_todo_task("משהו")
    assert out.startswith("❌")


def test_a_403_says_the_consent_is_the_problem(monkeypatch, api):
    def refuse(*a, **k):
        raise msgraph.GraphError(403, "Access denied")

    monkeypatch.setattr(msgraph, "graph", refuse)
    out = todo_tools.create_todo_task("משהו")
    assert "לאשר מחדש" in out


def test_the_connection_status_names_the_account(api):
    out = todo_tools.todo_connection_status()
    assert "itai@example.com" in out and "✅" in out


# --- the guard ----------------------------------------------------------


def test_the_module_never_touches_long_term_memory():
    """Same rule as msgraph: nothing here may write to storage.memory, which is
    rendered into the model's system prompt on every message."""
    tree = ast.parse(open(todo_tools.__file__, encoding="utf-8").read())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "save_memory" not in called


def test_every_tool_is_registered_with_the_model():
    """A tool the assistant cannot call is a tool that does not exist. This has
    already happened once in this project with a module that was written and
    never wired up."""
    import assistant

    registered = {getattr(f, "__name__", "") for f in assistant.tools_list}
    for tool, _ in EVERY_TOOL:
        assert tool.__name__ in registered


def test_a_duplicate_open_task_is_not_recreated(api):
    """A batch that died mid-loop gets retried whole; the tasks that already
    landed must not double."""
    out = todo_tools.create_todo_task("חלב", "קניות")
    assert "כבר קיימת" in out
    assert all(not (m == "POST" and p.endswith("/tasks")) for m, p, _ in api.writes)


def test_a_completed_task_with_the_same_title_does_not_block(api):
    """"להזמין רכב לטיפול" sits completed on the default list - wanting a fresh
    open one is exactly the point of recreating it."""
    out = todo_tools.create_todo_task("להזמין רכב לטיפול")
    assert "✅" in out
