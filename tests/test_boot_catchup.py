"""The boot catch-up re-answers one queued question outside any request, so a
message that was marked processed but never answered still gets its reply."""

import webhook_server


def test_catch_up_is_a_noop_without_the_env_pair(monkeypatch):
    monkeypatch.delenv("BOOT_REPLY_SENDER", raising=False)
    monkeypatch.delenv("BOOT_REPLY_TEXT", raising=False)
    monkeypatch.setattr(webhook_server.time, "sleep", lambda s: None)
    called = []
    monkeypatch.setattr(webhook_server, "handle_whatsapp_message",
                        lambda t, **kw: called.append(t) or "x")
    webhook_server._boot_catch_up()
    assert called == []


def test_catch_up_reasks_and_sends(monkeypatch):
    monkeypatch.setenv("BOOT_REPLY_SENDER", "972500000000")
    monkeypatch.setenv("BOOT_REPLY_TEXT", "מה יש לי בטודו?")
    monkeypatch.setattr(webhook_server.time, "sleep", lambda s: None)
    seen = {}

    def fake_handle(t, **kw):
        seen["asked"] = (t, kw)
        return "הרשימה"

    monkeypatch.setattr(webhook_server, "handle_whatsapp_message", fake_handle)
    monkeypatch.setattr(webhook_server, "_send_whatsapp_reply",
                        lambda to, text: seen.update(sent=(to, text)))
    webhook_server._boot_catch_up()
    assert seen["asked"][0] == "מה יש לי בטודו?"
    assert seen["asked"][1]["sender_id"] == "972500000000"
    # outside a request there is no 120s clock: the catch-up buys the
    # throttled tier real waiting room instead of another dead-end message
    assert seen["asked"][1]["wait_budget"] == 300.0
    assert seen["sent"] == ("972500000000", "הרשימה")
