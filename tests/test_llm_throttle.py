"""A short Retry-After is the minute's throttle, not the day's budget: wait it
out once, on the same provider, before asking the next one."""

import requests

import llm


class FakeResponse:
    def __init__(self, status=200, content="ענה", retry_after=None, body=None):
        self.status_code = status
        self.headers = {}
        if retry_after is not None:
            self.headers["retry-after"] = str(retry_after)
        self._body = body if body is not None else {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._body


def _patch(monkeypatch, script):
    seen = {"n": 0}
    monkeypatch.setattr(llm.time, "sleep", lambda s: seen.setdefault("sleeps", []).append(s))

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["n"] += 1
        return script[seen["n"] - 1]

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return seen


def test_short_retry_after_waits_and_wins(monkeypatch):
    seen = _patch(monkeypatch, [FakeResponse(429, retry_after=2), FakeResponse(200, "בוצע")])
    assert llm.ask("שאלה") == "בוצע"
    assert seen["n"] == 2
    assert seen["sleeps"] == [3.0]  # the asked 2s plus a breath


def test_a_long_retry_after_falls_through_without_sleeping(monkeypatch):
    seen = _patch(monkeypatch, [FakeResponse(429, retry_after=3600)])
    assert llm.ask("שאלה") is None
    assert seen["n"] == 1
    assert "sleeps" not in seen


def test_a_429_with_no_header_falls_through_immediately(monkeypatch):
    seen = _patch(monkeypatch, [FakeResponse(429)])
    assert llm.ask("שאלה") is None
    assert "sleeps" not in seen


def test_tool_loop_waits_out_a_throttle_mid_batch(monkeypatch):
    tools = [{"type": "function", "function": {"name": "t", "description": "d",
              "parameters": {"type": "object", "properties": {}, "required": []}}}]
    call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    script = [
        FakeResponse(200, body={"choices": [{"message": {"tool_calls": [call]}}]}),
        FakeResponse(429, retry_after=1),
        FakeResponse(200, body={"choices": [{"message": {"content": "סיימתי"}}]}),
    ]

    class FR(FakeResponse):
        pass

    seen = {"n": 0}
    monkeypatch.setattr(llm.time, "sleep", lambda s: seen.setdefault("sleeps", []).append(s))

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["n"] += 1
        return script[seen["n"] - 1]

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "q")
    answer = llm.ask_with_tools("תעשה", None, tools, lambda n, a: "ok")
    assert answer == "סיימתי"
    assert seen["sleeps"] == [2.0]
