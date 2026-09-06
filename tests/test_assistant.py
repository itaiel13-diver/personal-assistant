import json
import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

import assistant


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Every test gets its own memory file and a clean session cache."""
    monkeypatch.setattr(assistant, "MEMORY_FILE", str(tmp_path / "long_term_memory.json"))
    assistant._fallback_sessions.clear()
    yield
    assistant._fallback_sessions.clear()


def test_save_to_long_term_memory_persists_and_reports_success():
    result = assistant.save_to_long_term_memory("KSP עקרון", "KSP קרית עקרון", "store_mapping")
    assert "עודכן בהצלחה" in result
    with open(assistant.MEMORY_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert data["KSP עקרון"]["value"] == "KSP קרית עקרון"


def test_load_memory_context_empty_when_no_file():
    assert assistant._load_memory_context() == ""


def test_load_memory_context_includes_saved_facts():
    assistant.save_to_long_term_memory("k", "v")
    ctx = assistant._load_memory_context()
    assert "k" in ctx and "v" in ctx


def test_get_session_reuses_same_chat_for_same_sender(monkeypatch):
    # patch.object on the SDK's Chats object silently no-ops (it appears to
    # reject instance-level attribute overrides) - swap the whole client
    # reference instead, which is the boundary assistant.py actually owns.
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = lambda **kwargs: MagicMock()
    monkeypatch.setattr(assistant, "client", fake_client)

    chat1 = assistant._get_session("sender-a")
    chat2 = assistant._get_session("sender-a")
    assert chat1 is chat2
    assert fake_client.chats.create.call_count == 1


def test_get_session_is_rebuilt_when_the_date_changes(monkeypatch):
    """The current date is baked into the system instruction, so a session that
    survived midnight would keep believing 'today' is yesterday and schedule
    calendar events on the wrong day."""
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = lambda **kwargs: MagicMock()
    monkeypatch.setattr(assistant, "client", fake_client)

    assistant._get_session("sender-a")
    assert fake_client.chats.create.call_count == 1

    # Simulate the clock rolling into the next day.
    stale_chat, stale_date = assistant._fallback_sessions["sender-a"]
    assistant._fallback_sessions["sender-a"] = (stale_chat, stale_date - timedelta(days=1))

    assistant._get_session("sender-a")
    assert fake_client.chats.create.call_count == 2


def test_get_session_isolates_different_senders(monkeypatch):
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = lambda **kwargs: MagicMock()
    monkeypatch.setattr(assistant, "client", fake_client)

    chat_a = assistant._get_session("sender-a")
    chat_b = assistant._get_session("sender-b")
    assert chat_a is not chat_b


def test_history_is_loaded_from_storage_and_saved_back(monkeypatch):
    """This is the whole point of the storage layer: a fresh process must pick the
    conversation back up, and must write the new turns back for the next one."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    stored = [{"role": "user", "parts": [{"text": "שלום"}]}]
    monkeypatch.setattr(assistant.storage, "load_history", lambda sid: stored)
    saved = {}
    monkeypatch.setattr(assistant.storage, "save_history", lambda sid, h: saved.update({sid: h}))

    fake_chat = MagicMock()
    fake_chat.send_message.return_value = MagicMock(text="תשובה")
    fake_chat.get_history.return_value = [
        assistant.types.Content(role="user", parts=[assistant.types.Part(text="שלום")]),
        assistant.types.Content(role="model", parts=[assistant.types.Part(text="תשובה")]),
    ]
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    assistant.handle_whatsapp_message("מה קורה", sender_id="sender-db")

    # The stored history must be handed to the new chat...
    passed_history = fake_client.chats.create.call_args.kwargs["history"]
    assert len(passed_history) == 1
    assert passed_history[0].parts[0].text == "שלום"
    # ...and the updated history written back.
    assert "sender-db" in saved
    assert len(saved["sender-db"]) == 2


def test_no_process_state_is_kept_when_storage_is_enabled(monkeypatch):
    """With a database, nothing may be cached in process memory - that cache was
    exactly what made the assistant amnesiac after the server slept."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "load_history", lambda sid: [])
    monkeypatch.setattr(assistant.storage, "save_history", lambda sid, h: None)
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = lambda **kwargs: MagicMock()
    monkeypatch.setattr(assistant, "client", fake_client)

    assistant._get_session("sender-db")
    assistant._get_session("sender-db")
    assert assistant._fallback_sessions == {}
    assert fake_client.chats.create.call_count == 2  # rebuilt from storage each time


def test_one_corrupt_history_entry_does_not_lose_the_rest(monkeypatch):
    restored = assistant._deserialise([
        {"role": "user", "parts": [{"text": "טוב"}]},
        {"role": "user", "parts": "this is not valid"},
    ])
    assert len(restored) == 1
    assert restored[0].parts[0].text == "טוב"


def test_send_with_retry_recovers_after_transient_errors(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    chat = MagicMock()
    err = assistant.genai_errors.ServerError(503, {"error": {"message": "down"}}, MagicMock())
    chat.send_message.side_effect = [err, err, "ok-response"]
    result = assistant._send_with_retry(chat, "hi", attempts=3)
    assert result == "ok-response"
    assert chat.send_message.call_count == 3


def test_send_with_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    chat = MagicMock()
    err = assistant.genai_errors.ServerError(503, {"error": {"message": "down"}}, MagicMock())
    chat.send_message.side_effect = err
    with pytest.raises(assistant.genai_errors.ServerError):
        assistant._send_with_retry(chat, "hi", attempts=2)
    assert chat.send_message.call_count == 2


def test_handle_whatsapp_message_returns_hebrew_fallback_when_session_creation_fails(monkeypatch):
    """A brand-new sender whose very first session creation fails must still get the
    graceful fallback, not a raw exception - this is a distinct code path from a
    send_message failure on an already-open session."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
    err = assistant.genai_errors.ServerError(503, {"error": {"message": "down"}}, MagicMock())
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = err
    monkeypatch.setattr(assistant, "client", fake_client)

    result = assistant.handle_whatsapp_message("test", sender_id="brand-new-sender")
    assert "תקלה זמנית" in result
    assert "brand-new-sender" not in assistant._fallback_sessions  # no half-broken state left behind


def test_handle_whatsapp_message_falls_back_when_response_has_no_text(monkeypatch):
    """response.text is None (not an exception) for a safety-blocked or
    non-text-only response. Sending None onward would reach WhatsApp as a
    null body and fail silently - must substitute a real string instead."""
    fake_chat = MagicMock()
    fake_response = MagicMock()
    fake_response.text = None
    fake_chat.send_message.return_value = fake_response
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    result = assistant.handle_whatsapp_message("test", sender_id="sender-y")
    assert isinstance(result, str) and len(result) > 0


def test_quota_exhaustion_says_quota_not_try_again_in_a_moment(monkeypatch):
    """A free-tier 429 is a daily quota - it will not clear on a retry, so the
    generic 'temporary, try again in a moment' message would be misleading."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_chat = MagicMock()
    err = assistant.genai_errors.ClientError(429, {"error": {"message": "quota"}}, MagicMock())
    fake_chat.send_message.side_effect = err
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    result = assistant.handle_whatsapp_message("test", sender_id="sender-q")
    assert "מכסת השימוש היומית" in result
    assert "בעוד רגע" not in result


def test_handle_whatsapp_message_returns_hebrew_fallback_on_persistent_failure(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_chat = MagicMock()
    err = assistant.genai_errors.ServerError(503, {"error": {"message": "down"}}, MagicMock())
    fake_chat.send_message.side_effect = err
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    result = assistant.handle_whatsapp_message("test", sender_id="sender-x")
    assert "תקלה זמנית" in result
    assert fake_chat.send_message.call_count == 3  # exhausted all retry attempts
