"""Tests for the internet tools.

The network is never touched here. What is being checked is the wiring and the
failure behaviour: that a quota refusal becomes an answer Itai can act on rather
than a stack trace, that a bad URL is repaired before it is sent, and that the
web tools cannot be mistaken for the mail tools.
"""
import os
import sys
import types as pytypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import web_tools


class _FakeWeb:
    """Stands in for a grounding chunk's web field."""

    def __init__(self, title, uri):
        self.title = title
        self.uri = uri


def _response(text, sources=()):
    chunks = [pytypes.SimpleNamespace(web=_FakeWeb(t, u)) for t, u in sources]
    meta = pytypes.SimpleNamespace(grounding_chunks=chunks)
    candidate = pytypes.SimpleNamespace(grounding_metadata=meta)
    return pytypes.SimpleNamespace(text=text, candidates=[candidate])


@pytest.fixture
def captured(monkeypatch):
    """Replaces the Gemini call, recording what it was asked."""
    calls = []

    def fake(prompt, tool, label):
        calls.append({"prompt": prompt, "tool": tool, "label": label})
        return "תשובה"

    monkeypatch.setattr(web_tools, "_ask", fake)
    return calls


def test_search_passes_the_question_through(captured):
    web_tools.search_web("מתי המשחק של הפועל תל אביב")
    assert captured[0]["prompt"] == "מתי המשחק של הפועל תל אביב"
    assert captured[0]["tool"].google_search is not None


def test_reading_a_page_sends_the_url_and_the_question(captured):
    web_tools.read_web_page("https://example.com/x", "מה המחיר")
    assert "https://example.com/x" in captured[0]["prompt"]
    assert "מה המחיר" in captured[0]["prompt"]
    assert captured[0]["tool"].url_context is not None


def test_a_url_without_a_scheme_is_repaired(captured):
    # Itai types addresses the way he reads them; the fetcher needs a scheme.
    web_tools.read_web_page("ynet.co.il/news")
    assert "https://ynet.co.il/news" in captured[0]["prompt"]


def test_a_page_with_no_question_is_summarised(captured):
    web_tools.read_web_page("https://example.com")
    assert "סכם" in captured[0]["prompt"]


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_request_asks_for_one_instead_of_calling_out(empty, captured):
    assert "צריך" in web_tools.search_web(empty)
    assert "צריך" in web_tools.read_web_page(empty)
    assert captured == []


def test_a_quota_refusal_explains_itself_instead_of_leaking_a_stack_trace(monkeypatch):
    # Measured against the live API: search grounding is billed separately and
    # returns 429 on the free tier. Itai has to be able to tell that apart from
    # a broken assistant, because the fix is a billing decision, not a retry.
    def boom(*_a, **_k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")

    monkeypatch.setattr(web_tools, "_ask", boom)
    answer = web_tools.search_web("מה קורה בחדשות")
    assert "בתשלום" in answer
    assert "429" not in answer
    assert "RESOURCE_EXHAUSTED" not in answer


def test_an_ordinary_failure_still_says_what_went_wrong(monkeypatch):
    monkeypatch.setattr(web_tools, "_ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "boom" in web_tools.search_web("שאלה")
    assert "boom" in web_tools.read_web_page("https://example.com")


def test_sources_are_listed_under_the_answer(monkeypatch):
    monkeypatch.setattr(web_tools, "_get_client", lambda: pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=lambda **_k: _response(
            "הפועל משחקת מחר", [("ONE", "https://one.co.il"), ("Ynet", "https://ynet.co.il")]))))
    answer = web_tools.search_web("מתי המשחק")
    assert "הפועל משחקת מחר" in answer
    assert "ONE" in answer and "Ynet" in answer


def test_a_missing_citation_does_not_break_the_answer(monkeypatch):
    # Every grounding field is optional and the shape has moved between SDK
    # versions. Losing a source line is acceptable; raising mid-reply is not.
    broken = pytypes.SimpleNamespace(text="תשובה", candidates=[
        pytypes.SimpleNamespace(grounding_metadata=None)])
    monkeypatch.setattr(web_tools, "_get_client", lambda: pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=lambda **_k: broken)))
    assert web_tools.search_web("שאלה") == "תשובה"


def test_the_same_source_is_not_listed_twice(monkeypatch):
    monkeypatch.setattr(web_tools, "_get_client", lambda: pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=lambda **_k: _response(
            "כן", [("ONE", "https://one.co.il")] * 4))))
    assert web_tools.search_web("שאלה").count("ONE") == 1


def test_an_empty_model_answer_is_reported_not_returned_blank(monkeypatch):
    monkeypatch.setattr(web_tools, "_get_client", lambda: pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=lambda **_k: _response("   "))))
    assert "לא הצלחתי" in web_tools.search_web("שאלה")


def test_a_very_long_answer_is_cut_to_the_budget(monkeypatch):
    long_text = "\n".join(f"שורה {i}" for i in range(3000))
    monkeypatch.setattr(web_tools, "_get_client", lambda: pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=lambda **_k: _response(long_text))))
    answer = web_tools.search_web("שאלה")
    assert len(answer) <= web_tools.MAX_ANSWER_CHARS + 200
    assert "[קוצר]" in answer


def test_the_web_model_follows_the_conversation_model(monkeypatch):
    # Defaulting to a different model cost a test round to a 429 that had nothing
    # to do with the web - the model in production had quota, the default did not.
    monkeypatch.setenv("GEMINI_MODEL_NAME", "gemini-something-else")
    monkeypatch.delenv("GEMINI_WEB_MODEL_NAME", raising=False)
    import importlib
    reloaded = importlib.reload(web_tools)
    assert reloaded.WEB_MODEL_NAME == "gemini-something-else"
    importlib.reload(web_tools)


def test_the_web_module_cannot_send_anything():
    # The same guarantee the mail tools carry: this module reads, and that is all.
    import ast
    tree = ast.parse(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web_tools.py"), encoding="utf-8").read())
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "send" not in called
    assert "post" not in called
