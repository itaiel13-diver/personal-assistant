"""The Microsoft connection, and in particular the one thing about it that is
easy to get wrong: the refresh token rotates, and the rotated one has to be
stored or the connection dies days later for no visible reason.
"""

import ast
import time

import pytest

import msgraph
import storage


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    msgraph.reset()
    monkeypatch.setenv("MS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MS_REFRESH_TOKEN", "seed-token")
    # No database by default: the seed is then the whole story.
    monkeypatch.setattr(storage, "load_token", lambda provider: "")
    monkeypatch.setattr(storage, "save_token", lambda provider, token: False)
    yield
    msgraph.reset()


class Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def token_reply(**extra):
    return {"access_token": "at-1", "expires_in": 3600, **extra}


# --- which token is the live one ---------------------------------------


def test_the_seed_is_used_when_nothing_is_stored():
    assert msgraph.refresh_token() == "seed-token"


def test_a_stored_token_beats_the_seed(monkeypatch):
    monkeypatch.setattr(storage, "load_token", lambda provider: "stored-token")
    assert msgraph.refresh_token() == "stored-token"


def test_it_is_not_configured_without_a_token(monkeypatch):
    monkeypatch.delenv("MS_REFRESH_TOKEN")
    assert msgraph.configured() is False


def test_it_is_not_configured_without_a_secret(monkeypatch):
    monkeypatch.delenv("MS_CLIENT_SECRET")
    assert msgraph.configured() is False


def test_configured_when_all_three_are_present():
    assert msgraph.configured() is True


# --- the rotation ------------------------------------------------------


def test_a_rotated_refresh_token_is_stored(monkeypatch):
    saved = {}
    monkeypatch.setattr(storage, "save_token", lambda p, t: saved.setdefault(p, t) or True)
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: token_reply(refresh_token="new-token"))
    msgraph.access_token()
    assert saved == {"microsoft": "new-token"}


def test_an_unchanged_refresh_token_is_not_rewritten(monkeypatch):
    saves = []
    monkeypatch.setattr(storage, "save_token", lambda p, t: saves.append(t) or True)
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: token_reply(refresh_token="seed-token"))
    msgraph.access_token()
    assert saves == []


def test_a_refresh_that_returns_no_new_token_is_fine(monkeypatch):
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: token_reply())
    assert msgraph.access_token() == "at-1"


def test_a_rotation_that_cannot_be_stored_still_returns_a_working_token(monkeypatch, caplog):
    """The token we just used is still valid for now, so failing the call would
    be worse than logging. But it must be logged - this is the failure that
    otherwise surfaces days later as an unexplained invalid_grant."""
    monkeypatch.setattr(storage, "save_token", lambda p, t: False)
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: token_reply(refresh_token="new-token"))
    assert msgraph.access_token() == "at-1"
    assert any("could NOT be stored" in r.message for r in caplog.records)


def test_the_refresh_asks_for_every_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: seen.update(payload) or token_reply())
    msgraph.access_token()
    assert seen["scope"].split() == msgraph.SCOPES
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "seed-token"


def test_the_scopes_are_the_shared_list():
    import microsoft_scopes

    assert msgraph.SCOPES is microsoft_scopes.SCOPES


def test_offline_access_is_in_the_scopes():
    """Without it Microsoft issues no refresh token at all and the connection
    lasts exactly one hour."""
    assert "offline_access" in msgraph.SCOPES


def test_the_widest_task_scopes_are_asked_for():
    assert "Tasks.ReadWrite" in msgraph.SCOPES
    assert "Tasks.ReadWrite.Shared" in msgraph.SCOPES


# --- caching -----------------------------------------------------------


def test_the_access_token_is_reused_rather_than_refetched(monkeypatch):
    calls = []
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: calls.append(1) or token_reply())
    msgraph.access_token()
    msgraph.access_token()
    assert len(calls) == 1


def test_an_expired_access_token_is_refetched(monkeypatch):
    calls = []
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: calls.append(1) or token_reply())
    clock = [1000.0]
    monkeypatch.setattr(msgraph.time, "time", lambda: clock[0])
    msgraph.access_token()
    clock[0] += 4000
    msgraph.access_token()
    assert len(calls) == 2


def test_a_token_is_refreshed_before_it_actually_expires(monkeypatch):
    """The margin is the point: a token that expires between the check and the
    call is a 401 for no reason anyone can reproduce."""
    calls = []
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: calls.append(1) or token_reply())
    clock = [1000.0]
    monkeypatch.setattr(msgraph.time, "time", lambda: clock[0])
    msgraph.access_token()
    clock[0] += 3600 - msgraph.EXPIRY_MARGIN_SECONDS + 1
    msgraph.access_token()
    assert len(calls) == 2


def test_a_zero_lifetime_reply_does_not_cause_a_refresh_storm(monkeypatch):
    """Whatever Microsoft says, the token is held for at least a minute -
    otherwise a bad expires_in turns every Graph call into two."""
    calls = []
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: calls.append(1) or token_reply(expires_in=0))
    msgraph.access_token()
    msgraph.access_token()
    assert len(calls) == 1


def test_reset_forgets_the_cached_token(monkeypatch):
    calls = []
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: calls.append(1) or token_reply())
    msgraph.access_token()
    msgraph.reset()
    msgraph.access_token()
    assert len(calls) == 2


def test_an_unconfigured_connection_raises_rather_than_calling_out(monkeypatch):
    monkeypatch.delenv("MS_REFRESH_TOKEN")
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: pytest.fail("must not be called"))
    with pytest.raises(msgraph.GraphError):
        msgraph.access_token()


def test_a_reply_with_no_access_token_raises(monkeypatch):
    monkeypatch.setattr(msgraph, "_post_token", lambda payload: {"expires_in": 3600})
    with pytest.raises(msgraph.GraphError):
        msgraph.access_token()


# --- the request helper ------------------------------------------------


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(msgraph, "access_token", lambda: "at-1")


def http(monkeypatch, *responses):
    """Installs requests.request, returning the given responses in order."""
    seen = []
    queue = list(responses)

    class FakeRequests:
        @staticmethod
        def request(method, url, **kwargs):
            seen.append((method, url, kwargs))
            return queue.pop(0)

        @staticmethod
        def get(url, **kwargs):
            seen.append(("GET", url, kwargs))
            return queue.pop(0)

        @staticmethod
        def post(url, **kwargs):
            seen.append(("POST", url, kwargs))
            return queue.pop(0)

    import sys

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    return seen


def test_a_relative_path_becomes_a_graph_url(monkeypatch, token):
    seen = http(monkeypatch, Response(200, {"value": []}))
    msgraph.graph("GET", "/me/todo/lists")
    assert seen[0][1] == "https://graph.microsoft.com/v1.0/me/todo/lists"


def test_the_bearer_token_is_sent(monkeypatch, token):
    seen = http(monkeypatch, Response(200, {"ok": True}))
    msgraph.graph("GET", "/me")
    assert seen[0][2]["headers"]["Authorization"] == "Bearer at-1"


def test_a_204_is_an_empty_dict_not_a_crash(monkeypatch, token):
    http(monkeypatch, Response(204))
    assert msgraph.graph("DELETE", "/me/todo/lists/1") == {}


def test_an_error_carries_graphs_own_message(monkeypatch, token):
    http(monkeypatch, Response(404, {"error": {"message": "Item not found"}}))
    with pytest.raises(msgraph.GraphError) as caught:
        msgraph.graph("GET", "/me/todo/lists/nope")
    assert "Item not found" in str(caught.value)
    assert caught.value.status == 404


def test_a_401_is_retried_once_with_a_fresh_token(monkeypatch):
    """A cached token can be revoked rather than merely expired, and the retry
    is what tells the two apart."""
    tokens = iter(["stale", "fresh"])
    monkeypatch.setattr(msgraph, "access_token", lambda: next(tokens))
    seen = http(monkeypatch, Response(401, text="expired"), Response(200, {"ok": True}))
    assert msgraph.graph("GET", "/me") == {"ok": True}
    assert seen[0][2]["headers"]["Authorization"] == "Bearer stale"
    assert seen[1][2]["headers"]["Authorization"] == "Bearer fresh"


def test_a_second_401_gives_up(monkeypatch, token):
    http(monkeypatch, Response(401, text="expired"), Response(401, text="expired"))
    with pytest.raises(msgraph.GraphError) as caught:
        msgraph.graph("GET", "/me")
    assert caught.value.status == 401


# --- paging ------------------------------------------------------------


def test_every_page_is_read(monkeypatch, token):
    """A single page looks exactly like a complete answer, which is how an
    assistant reports that a task is not on a list it only read part of."""
    http(
        monkeypatch,
        Response(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"}),
        Response(200, {"value": [{"id": "2"}]}),
    )
    assert [i["id"] for i in msgraph.get_all("/me/todo/lists")] == ["1", "2"]


def test_paging_stops_at_the_page_limit(monkeypatch, token):
    endless = [
        Response(200, {"value": [{"id": str(i)}], "@odata.nextLink": "https://graph/next"})
        for i in range(20)
    ]
    http(monkeypatch, *endless)
    assert len(msgraph.get_all("/me/todo/lists", max_pages=3)) == 3


def test_a_failing_next_page_returns_what_was_read(monkeypatch, token):
    http(
        monkeypatch,
        Response(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"}),
        Response(500, text="boom"),
    )
    assert [i["id"] for i in msgraph.get_all("/me/todo/lists")] == ["1"]


# --- the guard ---------------------------------------------------------


def test_the_token_never_goes_into_the_memory_table():
    """storage.memory is injected verbatim into the model's system prompt on
    every message by assistant._load_memory_context. A credential written there
    is a credential shown to the model, and through it to whoever is talking to
    it, on every single turn. This test is the barrier - do not relax it to make
    a later feature fit."""
    tree = ast.parse(open(msgraph.__file__, encoding="utf-8").read())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "save_memory" not in called
    assert "load_memory" not in called
