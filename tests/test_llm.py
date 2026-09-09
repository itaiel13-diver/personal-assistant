"""The waterfall exists to survive a provider saying no, so that is what these test."""

import json

import pytest
import requests

import llm


class FakeResponse:
    def __init__(self, status=200, content="בסדר גמור", body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "choices": [{"message": {"content": content}}]
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._body


@pytest.fixture
def keys(monkeypatch):
    """All three providers configured, so order alone decides who answers."""
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")


@pytest.fixture
def calls(monkeypatch):
    """Records every provider asked, and replies with whatever the test queued."""
    seen = []
    queue = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append({"url": url, "model": (json or {}).get("model"),
                     "messages": (json or {}).get("messages"), "body": json})
        reply = queue.pop(0) if queue else FakeResponse()
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(requests, "post", fake_post)
    return {"seen": seen, "queue": queue}


def test_the_first_provider_with_a_key_answers(keys, calls):
    assert llm.ask("שאלה") == "בסדר גמור"
    assert len(calls["seen"]) == 1
    assert "generativelanguage" in calls["seen"][0]["url"]


def test_a_quota_refusal_falls_through_to_the_next_free_tier(keys, calls):
    """The whole point of the module: 429 on Gemini is not the end of the day."""
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(content="עניתי מ-Groq"))
    assert llm.ask("שאלה") == "עניתי מ-Groq"
    assert "api.groq.com" in calls["seen"][1]["url"]


def test_it_keeps_falling_through_to_the_last_provider(keys, calls):
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(content="openrouter כאן"))
    assert llm.ask("שאלה") == "openrouter כאן"
    assert len(calls["seen"]) == 3


def test_a_network_failure_is_treated_like_a_refusal(keys, calls):
    calls["queue"].append(requests.ConnectionError("no route"))
    calls["queue"].append(FakeResponse(content="הבא בתור"))
    assert llm.ask("שאלה") == "הבא בתור"


def test_an_empty_answer_falls_through_rather_than_being_returned(keys, calls):
    """An empty string would reach WhatsApp as a blank message - worse than silence."""
    calls["queue"].append(FakeResponse(content=""))
    calls["queue"].append(FakeResponse(content="תשובה אמיתית"))
    assert llm.ask("שאלה") == "תשובה אמיתית"


def test_every_provider_failing_returns_none_and_does_not_raise(keys, calls):
    """Callers are heartbeat routines - a dead model must make them quiet, not crash."""
    for _ in range(3):
        calls["queue"].append(FakeResponse(status=429))
    assert llm.ask("שאלה") is None


def test_providers_without_a_key_are_skipped_entirely(monkeypatch, calls):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "q")
    assert llm.ask("שאלה") == "בסדר גמור"
    assert len(calls["seen"]) == 1
    assert "api.groq.com" in calls["seen"][0]["url"]


def test_no_keys_at_all_returns_none_without_calling_anything(monkeypatch, calls):
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert llm.ask("שאלה") is None
    assert calls["seen"] == []


def test_skip_passes_over_a_provider_that_already_said_no(keys, calls):
    """The conversation has already had its 429 from Gemini's own SDK; asking it
    again over HTTP would spend a round trip to hear the same answer."""
    assert llm.ask("שאלה", skip=("gemini",)) == "בסדר גמור"
    assert "api.groq.com" in calls["seen"][0]["url"]


def test_the_system_prompt_is_sent_as_a_system_message(keys, calls):
    llm.ask("שאלה", system="אתה עוזר")
    messages = calls["seen"][0]["messages"]
    assert messages[0] == {"role": "system", "content": "אתה עוזר"}
    assert messages[1]["content"] == "שאלה"


def test_available_lists_only_configured_providers(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert llm.available() == ["groq"]


def test_a_model_name_can_be_overridden_from_the_environment(keys, calls, monkeypatch):
    monkeypatch.setenv("GROQ_MODEL_NAME", "llama-3.1-8b-instant")
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(content="ok"))
    llm.ask("שאלה")
    assert calls["seen"][1]["model"] == "llama-3.1-8b-instant"


def test_ask_json_parses_a_plain_object(keys, calls):
    calls["queue"].append(FakeResponse(content='{"drawer": "notify"}'))
    assert llm.ask_json("שאלה") == {"drawer": "notify"}


def test_ask_json_survives_a_code_fence(keys, calls):
    """Small models fence their JSON however firmly they are told not to."""
    calls["queue"].append(FakeResponse(content='```json\n{"drawer": "ignore"}\n```'))
    assert llm.ask_json("שאלה") == {"drawer": "ignore"}


def test_ask_json_survives_a_sentence_of_preamble(keys, calls):
    calls["queue"].append(FakeResponse(content='Sure! Here you go: {"drawer": "draft"} - hope that helps'))
    assert llm.ask_json("שאלה") == {"drawer": "draft"}


def test_ask_json_returns_none_for_an_unparseable_answer(keys, calls):
    calls["queue"].append(FakeResponse(content="I would rather explain it in words"))
    assert llm.ask_json("שאלה") is None


# --- photos -----------------------------------------------------------------


def test_a_photo_is_sent_inline_to_the_first_vision_provider(keys, calls):
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?") == "בסדר גמור"
    content = calls["seen"][0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[1] == {"type": "text", "text": "מה בתמונה?"}


def test_groq_gets_its_vision_model_not_the_text_default(keys, calls):
    """gpt-oss-120b cannot see; showing it a photo was the original bug."""
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(content="רואה"))
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?", skip=()) == "רואה"
    assert calls["seen"][1]["model"] == "qwen/qwen3.6-27b"
    assert "api.groq.com" in calls["seen"][1]["url"]


def test_a_text_only_provider_is_never_shown_a_photo(monkeypatch, calls):
    """OpenRouter's default free model is text-only, so with only its key set
    there is simply no vision tier - None, and not a single request."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?") is None
    assert calls["seen"] == []


def test_the_vision_model_can_be_overridden_from_the_environment(keys, calls, monkeypatch):
    """Groq's vision line churns; Render must be able to pin the next id."""
    monkeypatch.setenv("GROQ_VISION_MODEL_NAME", "vendor/next-vision-9b")
    llm.ask_image(b"px", "image/png", "מה בתמונה?", skip=("gemini",))
    assert calls["seen"][0]["model"] == "vendor/next-vision-9b"


def test_thinking_blocks_are_stripped_from_the_answer(keys, calls):
    calls["queue"].append(FakeResponse(content="<think>מרעיין לעצמו</think> התשובה"))
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?") == "התשובה"


def test_an_answer_that_is_only_thinking_falls_through(keys, calls):
    calls["queue"].append(FakeResponse(content="<think>רק חשיבה</think>"))
    calls["queue"].append(FakeResponse(content="תשובה אמיתית"))
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?") == "תשובה אמיתית"


def test_every_vision_provider_failing_returns_none(keys, calls):
    calls["queue"].append(FakeResponse(status=429))
    calls["queue"].append(FakeResponse(status=500))
    assert llm.ask_image(b"px", "image/jpeg", "מה בתמונה?") is None


def test_the_groq_vision_call_turns_off_thinking(keys, calls):
    """Thinking burns the free tier's whole output budget before the answer
    arrives; the vision call asks for a direct reply instead."""
    llm.ask_image(b"px", "image/jpeg", "מה בתמונה?", skip=("gemini",))
    assert calls["seen"][0]["body"].get("reasoning_effort") == "none"


# --- 413: too many tokens for the free minute --------------------------------


def test_a_413_retries_once_with_a_trimmed_prompt(keys, calls):
    """Groq's free tier refuses a request that alone exceeds the minute's
    token budget with 413. Trimming beats silence: same provider, smaller
    prompt, one retry."""
    calls["queue"].append(FakeResponse(status=413))
    calls["queue"].append(FakeResponse(content="עניתי אחרי קיצוץ"))
    fat = "ה" * 9000
    assert llm.ask(fat) == "עניתי אחרי קיצוץ"
    assert len(calls["seen"]) == 2
    first, second = calls["seen"][0], calls["seen"][1]
    assert first["url"] == second["url"], "the trim retries the same provider before falling through"
    trimmed = second["messages"][-1]["content"]
    assert len(trimmed) < len(fat)
    assert "cut to fit the token budget" in trimmed
    assert trimmed.endswith("ה" * 100), "the tail - the newest context - survives the cut"


def test_a_second_413_falls_through_to_the_next_provider(keys, calls):
    calls["queue"].append(FakeResponse(status=413))
    calls["queue"].append(FakeResponse(status=413))
    calls["queue"].append(FakeResponse(content="groq ענה"))
    assert llm.ask("שאלה") == "groq ענה"
    assert "api.groq.com" in calls["seen"][2]["url"]


def test_every_provider_413ing_returns_none(keys, calls):
    for _ in range(6):
        calls["queue"].append(FakeResponse(status=413))
    assert llm.ask("ה" * 9000) is None


# --- voice notes -------------------------------------------------------------


class _RecordingPost:
    """requests.post stand-in that also accepts multipart uploads."""

    def __init__(self):
        self.seen = []
        self.queue = []

    def __call__(self, url, headers=None, json=None, files=None, data=None, timeout=None):
        self.seen.append({"url": url, "files": files, "data": data})
        reply = self.queue.pop(0) if self.queue else FakeResponse(body={"text": "מה קורה"})
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_a_voice_note_is_transcribed_by_whisper_on_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "q")
    post = _RecordingPost()
    monkeypatch.setattr(requests, "post", post)

    assert llm.transcribe(b"ogg-bytes", "audio/ogg; codecs=opus") == "מה קורה"
    call = post.seen[0]
    assert "audio/transcriptions" in call["url"]
    assert call["data"]["model"] == "whisper-large-v3"
    assert call["files"]["file"][0] == "voice-note.ogg", "the endpoint sniffs the container from the extension"
    assert call["files"]["file"][1] == b"ogg-bytes"


def test_a_failed_transcription_falls_through_and_returns_none(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "q")
    post = _RecordingPost()
    post.queue.append(FakeResponse(status=429))
    monkeypatch.setattr(requests, "post", post)
    assert llm.transcribe(b"ogg-bytes", "audio/ogg") is None


def test_an_empty_transcript_counts_as_not_heard(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "q")
    post = _RecordingPost()
    post.queue.append(FakeResponse(body={"text": "  "}))
    monkeypatch.setattr(requests, "post", post)
    assert llm.transcribe(b"ogg-bytes", "audio/ogg") is None
