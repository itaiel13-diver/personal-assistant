"""The model-free maintenance route: secret-guarded, dry-run by GET, deletes
only on the explicit confirm action, refill goes through the dedupe guard."""

import webhook_server


def _client(monkeypatch):
    monkeypatch.setattr(webhook_server, "TICK_SECRET", "s3cret")
    webhook_server.app.config["TESTING"] = True
    return webhook_server.app.test_client()


def test_wrong_or_missing_key_is_a_404(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/admin/todo-maintenance").status_code == 404
    assert client.get("/admin/todo-maintenance?key=nope").status_code == 404


def test_get_returns_the_dry_run_plan(monkeypatch):
    client = _client(monkeypatch)
    seen = {}
    monkeypatch.setattr(webhook_server.todo_tools, "dedupe_todo_tasks",
                        lambda **kw: seen.update(kw) or "התוכנית")
    r = client.get("/admin/todo-maintenance?key=s3cret")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == "התוכנית"
    assert seen.get("confirm") is not True


def test_confirm_action_deletes(monkeypatch):
    client = _client(monkeypatch)
    seen = {}
    monkeypatch.setattr(webhook_server.todo_tools, "dedupe_todo_tasks",
                        lambda **kw: seen.update(kw) or "נמחקו")
    r = client.post("/admin/todo-maintenance", json={"key": "s3cret", "action": "dedupe-confirm"})
    assert r.get_data(as_text=True) == "נמחקו"
    assert seen.get("confirm") is True


def test_refill_creates_each_task_through_the_guard(monkeypatch):
    client = _client(monkeypatch)
    seen = []
    monkeypatch.setattr(webhook_server.todo_tools, "create_todo_task",
                        lambda **kw: seen.append(kw["title"]) or f"ok {kw['title']}")
    r = client.post("/admin/todo-maintenance", json={
        "key": "s3cret", "action": "refill",
        "tasks": [{"title": "א", "due": "2026-09-09", "importance": "high"}, {"title": "ב"}]})
    assert seen == ["א", "ב"]
    assert "ok א" in r.get_data(as_text=True)
