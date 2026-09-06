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


@pytest.fixture(autouse=True)
def no_search_key(monkeypatch):
    """Most tests here exercise the Gemini fallback, which only runs when no
    Tavily key is set. Without this the suite would pass or fail depending on
    whether the machine running it happens to have a key in its environment."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


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


def _web_tools_source():
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web_tools.py"), encoding="utf-8").read()


def test_the_web_module_cannot_send_anything():
    """The same guarantee the mail tools carry: this module reads, and that is all.

    It used to be enforced by forbidding an HTTP POST outright, which stopped
    being possible the day search moved to Tavily - a search request is a POST.
    So the rule is now about where the POST goes rather than whether one exists:
    the one destination is the search endpoint, and nothing here may address
    Itai's mailbox or WhatsApp. Dropping the test to make Tavily fit would have
    quietly retired the guarantee instead of restating it.
    """
    import ast

    source = _web_tools_source()
    tree = ast.parse(source)
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "send" not in called

    posts = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "post"]
    for node in posts:
        target = node.args[0] if node.args else None
        assert isinstance(target, ast.Name) and target.id == "TAVILY_URL", (
            "web_tools may only POST to the search endpoint"
        )

    for forbidden in ("graph.facebook.com", "gmail", "googleapis.com/gmail", "messages"):
        assert forbidden not in source.lower(), f"web_tools must not reference {forbidden}"


def test_the_only_address_this_module_posts_to_is_the_search_api():
    assert web_tools.TAVILY_URL == "https://api.tavily.com/search"


# --- Tavily, the free search backend -------------------------------------


class _FakeHTTP:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def tavily(monkeypatch):
    """Turns the Tavily path on and captures the request instead of sending it.

    Set .payload / .status before calling search_web to shape the reply.
    """
    import requests

    state = pytypes.SimpleNamespace(sent=[], payload={"answer": "תשובה", "results": []}, status=200)

    def fake_post(url, **kwargs):
        state.sent.append({"url": url, **kwargs})
        return _FakeHTTP(state.payload, state.status)

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setattr(requests, "post", fake_post)
    return state


def test_a_configured_key_is_used_instead_of_gemini_grounding(tavily, monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("Gemini grounding was called even though Tavily is configured")

    monkeypatch.setattr(web_tools, "_ask", must_not_run)
    assert web_tools.search_web("מזג האוויר מחר") == "תשובה"
    assert tavily.sent[0]["url"] == web_tools.TAVILY_URL
    assert tavily.sent[0]["json"]["query"] == "מזג האוויר מחר"


def test_the_key_travels_in_the_header_and_in_the_body(tavily):
    web_tools.search_web("שאלה")
    sent = tavily.sent[0]
    assert sent["headers"]["Authorization"] == "Bearer tvly-test-key"
    assert sent["json"]["api_key"] == "tvly-test-key"


def test_the_answer_comes_back_with_the_pages_behind_it(tavily):
    tavily.payload = {
        "answer": "המשחק ביום שבת",
        "results": [
            {"title": "אתר הפועל", "url": "https://a.example"},
            {"title": "ONE", "url": "https://b.example"},
        ],
    }
    out = web_tools.search_web("מתי המשחק")
    assert "המשחק ביום שבת" in out
    assert "אתר הפועל" in out and "ONE" in out


def test_a_source_listed_twice_appears_once(tavily):
    tavily.payload = {
        "answer": "כן",
        "results": [
            {"title": "ynet", "url": "https://ynet.co.il/1"},
            {"title": "ynet", "url": "https://ynet.co.il/2"},
        ],
    }
    assert web_tools.search_web("שאלה").count("ynet") == 1


def test_a_result_without_a_title_falls_back_to_its_url(tavily):
    tavily.payload = {"answer": "כן", "results": [{"url": "https://example.com/x"}]}
    assert "https://example.com/x" in web_tools.search_web("שאלה")


def test_snippets_stand_in_when_tavily_returns_no_summary(tavily):
    tavily.payload = {
        "answer": "",
        "results": [{"title": "מקור", "url": "https://x.example", "content": "פרט חשוב"}],
    }
    assert "פרט חשוב" in web_tools.search_web("שאלה")


def test_nothing_found_says_so_rather_than_returning_an_empty_message(tavily):
    tavily.payload = {"answer": "", "results": []}
    out = web_tools.search_web("שאלה בלי תשובה")
    assert "לא מצאתי" in out


def test_a_long_answer_is_capped_before_it_reaches_whatsapp(tavily):
    tavily.payload = {"answer": "\n".join(["שורה"] * 5000), "results": []}
    out = web_tools.search_web("שאלה")
    assert len(out) <= web_tools.MAX_ANSWER_CHARS + 100
    assert "[קוצר]" in out


def test_a_rejected_key_is_explained_instead_of_raising(tavily):
    tavily.status = 401
    out = web_tools.search_web("שאלה")
    assert "TAVILY_API_KEY" in out
    assert "401" not in out


def test_an_exhausted_quota_says_when_it_comes_back(tavily):
    tavily.status = 429
    out = web_tools.search_web("שאלה")
    assert "מכסת" in out and "חודש" in out


def test_a_network_failure_does_not_retry_the_path_known_to_be_blocked(tavily, monkeypatch):
    import requests

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(
        web_tools,
        "_ask",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell back to Gemini grounding")),
    )
    assert "נכשל" in web_tools.search_web("שאלה")


def test_without_a_key_the_refusal_names_the_free_alternative(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def quota_exhausted(*a, **k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(web_tools, "_ask", quota_exhausted)
    out = web_tools.search_web("שאלה")
    assert "Tavily" in out
    assert "429" not in out and "RESOURCE_EXHAUSTED" not in out
