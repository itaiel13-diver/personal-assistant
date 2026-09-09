"""The boot catch-up re-answers one queued question outside any request, so a
message that was marked processed but never answered still gets its reply."""

import webhook_server


def test_catch_up_is_a_noop_without_the_env_pair(monkeypatch):
    monkeypatch.delenv("BOOT_REPLY_SENDER", raising=False)
    monkeypatch.delenv("BOOT_REPLY_TEXT", raising=False)
    monkeypatch.setattr(webhook_server.time, "sleep", lambda s: None)
    called = []
    monkeypatch.setattr(webhook_server, "handle_whatsapp_message", lambda t, sender_id: called.append(t) or "x")
    webhook_server._boot_catch_up()
    assert called == []


def test_catch_up_reasks_and_sends(monkeypatch):
    monkeypatch.setenv("BOOT_REPLY_SENDER", "972500000000")
    monkeypatch.setenv("BOOT_REPLY_TEXT", "מה יש לי בטודו?")
    monkeypatch.setattr(webhook_server.time, "sleep", lambda s: None)
    seen = {}
    monkeypatch.setattr(webhook_server, "handle_whatsapp_message",
                        lambda t, sender_id: seen.update(asked=(t, sender_id)) or "הרשימה")
    monkeypatch.setattr(webhook_server, "_send_whatsapp_reply",
                        lambda to, text: seen.update(sent=(to, text)))
    webhook_server._boot_catch_up()
    assert seen["asked"] == ("מה יש לי בטודו?", "972500000000")
    assert seen["sent"] == ("972500000000", "הרשימה")
