"""The failed 10:04 flow, fixed: a per-minute 429 must wait and retry instead
of crying daily-quota; a bare "כן, תוסיף" must inherit the task talk from
recent history; a photo with an actionable caption must go from vision
extraction into the tool loop."""

from unittest.mock import MagicMock

import assistant
import tool_bridge


def _quota_error(delay_text: str):
    return assistant.genai_errors.ClientError(
        429, {"error": {"message": f"Quota exceeded. Please retry in {delay_text}."}},
        MagicMock())


def test_retry_delay_parsed_from_google_wording():
    err = _quota_error("33.690716314s")
    assert assistant._asked_retry_delay_seconds(err) == 33.690716314


def test_retry_delay_none_when_not_said():
    err = assistant.genai_errors.ClientError(400, {"error": {"message": "bad"}}, MagicMock())
    assert assistant._asked_retry_delay_seconds(err) is None


def test_minute_quota_waits_and_retries_once(monkeypatch):
    sleeps = []
    monkeypatch.setattr(assistant.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def send(_text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _quota_error("5s")
        return MagicMock()

    result = assistant._send_with_retry(MagicMock(send_message=send), "hi")
    assert result is not None
    assert calls["n"] == 2
    assert sleeps and sleeps[0] >= 6  # the asked 5s plus a breath


def test_daily_quota_raises_straight_to_the_fallback(monkeypatch):
    monkeypatch.setattr(assistant.time, "sleep", lambda s: None)
    chat = MagicMock()
    chat.send_message.side_effect = _quota_error("86399.9s")
    try:
        assistant._send_with_retry(chat, "hi")
        assert False, "should have raised"
    except assistant.genai_errors.ClientError:
        pass
    assert chat.send_message.call_count == 1  # no patient wait for a daily cap


def test_bare_yes_add_inherits_the_task_pack_from_history(monkeypatch):
    """"כן, תוסיף" alone names no tool; the history full of משימות must pull
    the todo pack so the tool loop runs instead of the tool-less answer."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "load_history", lambda sender: [
        {"role": "user", "parts": [{"text": "הנה המשימות מהתמונה"}]},
        {"role": "model", "parts": [{"text": "קיבלתי, 10 משימות לחנויות"}]},
    ])
    monkeypatch.setattr(assistant.storage, "append_user_turn", lambda s, t: None)
    monkeypatch.setattr(assistant.storage, "append_model_turn", lambda s, t: None)
    captured = {}

    def fake_ask_with_tools(prompt, system, tools, call_tool, **kwargs):
        captured["prompt"] = prompt
        captured["tools"] = tools
        return "הוספתי את כל המשימות"

    monkeypatch.setattr(assistant.llm, "ask_with_tools", fake_ask_with_tools)
    monkeypatch.setattr(assistant.llm, "ask", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("tool-less path must not run when a pack matched")))

    reply = assistant._answer_without_gemini("כן, תוסיף", "sender1")
    assert reply == "הוספתי את כל המשימות"
    names = [t["function"]["name"] for t in captured["tools"]]
    assert "create_todo_task" in names


def test_photo_with_action_caption_goes_through_the_tool_loop(monkeypatch):
    """Photo + "תוסיף את המשימות שבתמונה": the vision model reads the pixels,
    then the tool loop executes the caption against that reading."""
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "load_history", lambda sender: [])
    monkeypatch.setattr(assistant.storage, "append_user_turn", lambda s, t: None)
    monkeypatch.setattr(assistant.storage, "append_model_turn", lambda s, t: None)
    monkeypatch.setattr(assistant.llm, "ask_image",
                        lambda *a, **k: "בתמונה: משימה 1 - התקנת S26, משימה 2 - פירוק S25")
    captured = {}

    def fake_ask_with_tools(prompt, system, tools, call_tool, **kwargs):
        captured["prompt"] = prompt
        return "הוספתי 2 משימות לטודו"

    monkeypatch.setattr(assistant.llm, "ask_with_tools", fake_ask_with_tools)
    reply = assistant._describe_image_without_gemini(
        b"pixels", "image/jpeg", "תוסיף את המשימות שבתמונה לטודו", "sender1")
    assert reply == "הוספתי 2 משימות לטודו"
    assert "התקנת S26" in captured["prompt"]  # the extraction reached the loop


def test_photo_with_plain_caption_stays_a_description(monkeypatch):
    monkeypatch.setattr(assistant.storage, "enabled", lambda: True)
    monkeypatch.setattr(assistant.storage, "load_history", lambda sender: [])
    monkeypatch.setattr(assistant.storage, "append_user_turn", lambda s, t: None)
    monkeypatch.setattr(assistant.storage, "append_model_turn", lambda s, t: None)
    monkeypatch.setattr(assistant.llm, "ask_image", lambda *a, **k: "תמונה של חתול")
    monkeypatch.setattr(assistant.llm, "ask_with_tools", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no tools for a plain look-at-this caption")))
    reply = assistant._describe_image_without_gemini(
        b"pixels", "image/jpeg", "מה זה?", "sender1")
    assert reply == "תמונה של חתול"


def test_pack_selection_broader_list_word():
    assert "todo" in tool_bridge.select_packs("תוסיף את זה לרשימה שלי")
