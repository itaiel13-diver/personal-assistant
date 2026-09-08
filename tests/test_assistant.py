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


def test_every_incoming_message_gets_a_fresh_search_budget(monkeypatch):
    """The cap is per message, so something has to zero it per message. If this
    wiring is ever dropped, the first two searches of the day would work and
    every search after that would be refused - a failure that only shows up on
    the second question and looks like the internet being broken."""
    import web_tools

    fake_chat = MagicMock()
    fake_chat.send_message.return_value = MagicMock(text="בסדר")
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)
    monkeypatch.setattr(web_tools, "_ask", lambda *a, **k: "תשובה")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    web_tools.search_web("א")
    web_tools.search_web("ב")
    assert "נגמרו" in web_tools.search_web("ג")   # budget spent

    assistant.handle_whatsapp_message("שאלה חדשה", sender_id="sender-budget")
    assert web_tools.search_web("ד") == "תשובה"


def test_the_prompt_and_the_enforced_cap_say_the_same_number():
    """The prompt spells the limit out in words for the model, and web_tools
    enforces it as an integer. Nothing keeps the two in step automatically, so
    changing the constant has to fail here until the prompt is changed too."""
    import web_tools

    assert web_tools.MAX_SEARCHES_PER_MESSAGE == 2
    assert "TWO searches per message" in assistant.SYSTEM_PROMPT
    assert "ask Itai one short question instead of searching" in assistant.SYSTEM_PROMPT


def test_the_drive_tools_are_actually_registered():
    """A tool the model cannot see does not exist. drive_tools passing its own
    tests proves the module works, not that Gemini was ever offered it."""
    names = {t.__name__ for t in assistant.tools_list}
    assert {
        "search_drive",
        "list_drive_folder",
        "read_drive_file",
        "create_drive_file",
        "update_drive_file",
    } <= names


def test_the_prompt_and_the_toolbox_agree_about_sharing():
    """Sharing is the Drive power the code still refuses. If someone adds a tool
    for it without revisiting the prompt, or softens the prompt without adding
    the tool, one of these two halves is lying to Itai."""
    assert "You cannot share a file" in assistant.SYSTEM_PROMPT
    names = {t.__name__ for t in assistant.tools_list}
    assert not any("share" in n or "permission" in n for n in names)


def test_the_prompt_tells_the_model_the_bin_is_the_default_and_not_destruction():
    """Deleting became possible on 2026-09-07 at Itai's request. The prompt has
    to carry the shape of it, not just the fact: an ordinary removal goes to the
    bin, and permanent=True is something he asks for rather than something the
    model reaches for on its own."""
    prompt = assistant.SYSTEM_PROMPT
    assert "trash_drive_file" in prompt
    assert "recoverable for 30 days" in prompt
    assert "Never pass that" in prompt
    assert assistant.trash_drive_file in assistant.tools_list


def test_a_spare_free_tier_answers_when_gemini_is_out_of_quota(monkeypatch):
    """Hitting the 20/day Gemini cap at 11am used to mean no assistant until
    midnight. With a second free tier configured it means a reduced one."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_chat = MagicMock()
    fake_chat.send_message.side_effect = assistant.genai_errors.ClientError(
        429, {"error": {"message": "quota"}}, MagicMock()
    )
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    asked = {}

    def fake_ask(prompt, system="", **kwargs):
        asked["prompt"] = prompt
        asked["system"] = system
        asked["skip"] = kwargs.get("skip")
        return "עניתי בכל זאת"

    monkeypatch.setattr(assistant.llm, "ask", fake_ask)

    result = assistant.handle_whatsapp_message("מה שלומך", sender_id="sender-spare")
    assert result == "עניתי בכל זאת"
    assert "מכסת השימוש" not in result
    # Gemini has already refused on its own SDK; asking it again over HTTP
    # would spend a round trip to be told the same thing.
    assert asked["skip"] == ("gemini",)
    assert "מה שלומך" in asked["prompt"]


def test_the_fallback_tier_is_told_it_has_no_tools(monkeypatch):
    """It cannot read mail or the calendar, so it must say so rather than
    invent what is in them - the one failure mode that would be worse than
    the quota message it replaces."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_chat = MagicMock()
    fake_chat.send_message.side_effect = assistant.genai_errors.ClientError(
        429, {"error": {"message": "quota"}}, MagicMock()
    )
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)

    seen = {}

    def fake_ask(prompt, system="", **kwargs):
        seen["system"] = system
        return "תשובה"

    monkeypatch.setattr(assistant.llm, "ask", fake_ask)
    assistant.handle_whatsapp_message("תקרא לי מיילים", sender_id="sender-notools")
    assert "tools are unavailable" in seen["system"]


def test_quota_message_still_shows_when_no_spare_tier_answers(monkeypatch):
    """Every provider dry is the one case where the honest answer is still
    'come back tomorrow'."""
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_chat = MagicMock()
    fake_chat.send_message.side_effect = assistant.genai_errors.ClientError(
        429, {"error": {"message": "quota"}}, MagicMock()
    )
    fake_client = MagicMock()
    fake_client.chats.create.return_value = fake_chat
    monkeypatch.setattr(assistant, "client", fake_client)
    monkeypatch.setattr(assistant.llm, "ask", lambda *a, **k: None)

    result = assistant.handle_whatsapp_message("test", sender_id="sender-dry")
    assert "מכסת השימוש היומית" in result
