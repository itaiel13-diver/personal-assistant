"""dedupe_todo_tasks: same store, different wording - grouped, one kept, the
rest deleted by id, and only after he approves the plan."""

import todo_tools
from tests.test_todo_tools import Graph, LISTS

DUPE_TASKS = {
    "L1": [
        {"id": "KEEP1", "title": "מחסני חשמל רמלה - הרצל 91: התקנת Watch Ultra2 ELECTRA + פירוק S25 FE ELEC",
         "status": "notStarted", "createdDateTime": "2026-09-09T07:30:00Z"},
        {"id": "DUP1", "title": "מחסני חשמל רמלה - הרצל 91: התקנת Watch Ultra2",
         "status": "notStarted", "createdDateTime": "2026-09-09T07:31:00Z"},
        {"id": "DUP2", "title": "מחסני חשמל רמלה - הרצל 91",
         "status": "notStarted", "createdDateTime": "2026-09-09T07:32:00Z"},
        {"id": "SOLO", "title": "דינמיקה+ רחובות: התקנת S26 FE CEL",
         "status": "notStarted", "createdDateTime": "2026-09-09T07:33:00Z"},
    ],
    "L2": [],
    "L3": [],
}


def _patch(monkeypatch):
    fake = Graph()
    monkeypatch.setattr(todo_tools.msgraph, "configured", lambda: True)
    monkeypatch.setattr(todo_tools.msgraph, "get_all", fake.get_all)
    monkeypatch.setattr(todo_tools.msgraph, "graph", fake.graph)
    monkeypatch.setattr(todo_tools, "TASKS", DUPE_TASKS, raising=False)
    import tests.test_todo_tools as base
    monkeypatch.setattr(base, "TASKS", DUPE_TASKS)
    return fake


def test_dry_run_plans_without_deleting(monkeypatch):
    fake = _patch(monkeypatch)
    plan = todo_tools.dedupe_todo_tasks()
    assert "נמצאו 2 כפילויות" in plan
    assert "נשארת: מחסני חשמל רמלה - הרצל 91: התקנת Watch Ultra2 ELECTRA + פירוק S25 FE ELEC" in plan
    assert "...DUP1" in plan and "...DUP2" in plan
    assert "SOLO" not in plan  # singletons are not even mentioned
    assert fake.writes == []   # dry run touches nothing


def test_confirm_deletes_only_the_extras_by_id(monkeypatch):
    fake = _patch(monkeypatch)
    out = todo_tools.dedupe_todo_tasks(confirm=True)
    assert "נמחקו 2 כפילויות" in out
    deleted = [p for m, p, _ in fake.writes if m == "DELETE"]
    assert deleted == ["/me/todo/lists/L1/tasks/DUP1", "/me/todo/lists/L1/tasks/DUP2"]


def test_a_clean_list_says_so(monkeypatch):
    fake = _patch(monkeypatch)
    import tests.test_todo_tools as base
    monkeypatch.setattr(base, "TASKS", {"L1": [DUPE_TASKS["L1"][3]], "L2": [], "L3": []})
    assert "אין כפילויות" in todo_tools.dedupe_todo_tasks()
    assert fake.writes == []
