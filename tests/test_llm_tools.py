"""llm.ask_with_tools: the OpenAI-protocol tool loop on the spare tier. The
model drives, the loop executes, and the round cap plus the 413 trim keep
one confused model from eating the whole free daily budget."""

import requests

import llm


GROQ = next(p for p in llm.PROVIDERS if p["name"] == "groq")
TOOLS = [{
    "type": "function",
    "function": {
        "name": "add_task",
        "description": "Adds a task.",
        "parameters": {"type": "object",
                       "properties": {"title": {"type": "string"}},
                       "required": ["title"]},
    },
}]


def _body(tool_calls=None, content=None):
    message = {}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if content is not None:
        message["content"] = content
    return {"choices": [{"message": message}]}


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


def _patch(monkeypatch, script):
    """Each entry in the script is one response for one request, in order."""
    calls = {"n": 0, "bodies": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["bodies"].append(json)
        calls["n"] += 1
        entry = script[calls["n"] - 1]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, FakeResponse):
            return entry
        return FakeResponse(entry)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    return calls


def _tool_call(name="add_task", arguments='{"title": "חלב"}', call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def test_tool_call_executes_then_final_answer(monkeypatch):
    script = [
        _body(tool_calls=[_tool_call()]),
        _body(content="הוספתי את המשימה"),
    ]
    calls = _patch(monkeypatch, script)
    executed = []
    answer = llm.ask_with_tools("תוסיף חלב", "system", TOOLS,
                                lambda name, args: executed.append((name, args)) or "✅ נוסף")
    assert answer == "הוספתי את המשימה"
    assert executed == [("add_task", '{"title": "חלב"}')]
    # Second request carries the assistant tool_call and the tool result.
    second = calls["bodies"][1]["messages"]
    assert any(m.get("tool_calls") for m in second)
    assert any(m.get("role") == "tool" and m.get("content") == "✅ נוסף" for m in second)
    assert calls["bodies"][0]["tools"] == TOOLS


def test_plain_answer_needs_no_rounds(monkeypatch):
    _patch(monkeypatch, [_body(content="שלום")])
    assert llm.ask_with_tools("היי", None, TOOLS, lambda n, a: "x") == "שלום"


def test_thinking_is_stripped_from_the_answer(monkeypatch):
    _patch(monkeypatch, [_body(content="<think>hmm</think>תשובה")])
    assert llm.ask_with_tools("שאלה", None, TOOLS, lambda n, a: "x") == "תשובה"


def test_round_cap_returns_none(monkeypatch):
    script = [_body(tool_calls=[_tool_call(call_id=f"c{i}")]) for i in range(5)]
    calls = _patch(monkeypatch, script)
    answer = llm.ask_with_tools("לולאה", None, TOOLS, lambda n, a: "ok", max_rounds=5)
    assert answer is None
    assert calls["n"] == 5


def test_413_trims_and_retries_same_provider(monkeypatch):
    big_tool_result = "x" * 6000
    script = [
        _body(tool_calls=[_tool_call()]),
        FakeResponse(status=413),
        _body(content="בסוף ענה"),
    ]
    calls = _patch(monkeypatch, script)
    answer = llm.ask_with_tools("שאלה", None, TOOLS, lambda n, a: big_tool_result)
    assert answer == "בסוף ענה"
    # After the 413 the tool result is shrunk before the retry.
    retry_messages = calls["bodies"][2]["messages"]
    tool_message = next(m for m in retry_messages if m.get("role") == "tool")
    assert len(tool_message["content"]) < len(big_tool_result)


def test_413_twice_falls_through_to_none(monkeypatch):
    _patch(monkeypatch, [FakeResponse(status=413), FakeResponse(status=413)])
    assert llm.ask_with_tools("שאלה", None, TOOLS, lambda n, a: "ok") is None


def test_server_error_falls_through(monkeypatch):
    _patch(monkeypatch, [FakeResponse(status=500)])
    assert llm.ask_with_tools("שאלה", None, TOOLS, lambda n, a: "ok") is None


def test_provider_without_tools_flag_is_never_called(monkeypatch):
    """Gemini and OpenRouter carry "tools": False - schemas must never reach
    a provider that cannot act on them, even with a key set."""
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.ask_with_tools("שאלה", None, TOOLS, lambda n, a: "ok") is None
