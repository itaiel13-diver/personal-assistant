"""Throttle patience has a ceiling: a webhook worker dies at 120s, so waits
that would push the whole call past the budget are skipped, not slept."""

import requests

import llm
from tests.test_llm_throttle import FakeResponse


def _patch(monkeypatch, script):
    seen = {"n": 0}
    monkeypatch.setattr(llm.time, "sleep", lambda s: seen.setdefault("sleeps", []).append(s))

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["n"] += 1
        return script[min(seen["n"] - 1, len(script) - 1)]

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return seen


def test_ask_skips_a_wait_that_would_blow_the_budget(monkeypatch):
    monkeypatch.setattr(llm, "_WAIT_BUDGET_SECONDS", 10)
    seen = _patch(monkeypatch, [FakeResponse(429, retry_after=30), FakeResponse(200, "מעולה")])
    # the 30s wait would exceed the 10s budget: no sleep, no second try, None
    assert llm.ask("שאלה") is None
    assert seen["n"] == 1
    assert "sleeps" not in seen


def test_ask_still_waits_when_the_budget_allows(monkeypatch):
    monkeypatch.setattr(llm, "_WAIT_BUDGET_SECONDS", 90)
    seen = _patch(monkeypatch, [FakeResponse(429, retry_after=30), FakeResponse(200, "מעולה")])
    assert llm.ask("שאלה") == "מעולה"
    assert seen["sleeps"] == [31.0]


def test_tool_loop_stops_sleeping_once_the_budget_is_spent(monkeypatch):
    monkeypatch.setattr(llm, "_WAIT_BUDGET_SECONDS", 12)
    tools = [{"type": "function", "function": {"name": "t", "description": "d",
              "parameters": {"type": "object", "properties": {}, "required": []}}}]
    call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    script = [
        FakeResponse(429, retry_after=5),                    # waited (5+1 <= 12)
        FakeResponse(200, body={"choices": [{"message": {"tool_calls": [call]}}]}),
        FakeResponse(429, retry_after=30),                   # would blow the budget
        FakeResponse(200, body={"choices": [{"message": {"content": "לא אמור להגיע"}}]}),
    ]
    seen = _patch(monkeypatch, script)
    answer = llm.ask_with_tools("תעשה", None, tools, lambda n, a: "ok")
    assert answer is None
    assert seen["sleeps"] == [6.0]
    assert seen["n"] == 3  # the over-budget wait never became a request
