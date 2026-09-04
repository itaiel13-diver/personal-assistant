import json
import time
from unittest.mock import MagicMock

import pytest

import assistant


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Every test gets its own memory file and a clean session cache."""
    monkeypatch.setattr(assistant, "MEMORY_FILE", str(tmp_path / "long_term_memory.json"))
    assistant._sessions.clear()
    yield
    assistant._sessions.clear()


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


def test_get_session_isolates_different_senders(monkeypatch):
    fake_client = MagicMock()
    fake_client.chats.create.side_effect = lambda **kwargs: MagicMock()
    monkeypatch.setattr(assistant, "client", fake_client)

    chat_a = assistant._get_session("sender-a")
    chat_b = assistant._get_session("sender-b")
    assert chat_a is not chat_b


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
    assert "brand-new-sender" not in assistant._sessions  # no half-broken state left behind


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
