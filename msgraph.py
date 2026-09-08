"""The connection to Microsoft Graph: one access token, kept fresh, and one
request helper that every To Do call goes through.

Read this before touching the token handling, because Microsoft's refresh
tokens do not behave like Google's.

    Google gives one refresh token per consent and it stays valid until it is
    revoked. Ours lives in an environment variable and that is fine.

    Microsoft ROTATES the refresh token: every time one is redeemed, the reply
    carries a NEW refresh token with a fresh 90-day life, and the one just used
    is on its way out. The old one keeps working for a short grace period, so
    nothing breaks immediately - which is exactly the trap. An assistant that
    reads its token from an environment variable and throws away the rotated
    one appears to work for days and then stops, at a moment unconnected to any
    deploy, with a plain invalid_grant.

So the live token is stored in the database (storage.save_token), and the
environment variable MS_REFRESH_TOKEN is only a SEED - the value written once
after Itai approves in the browser, used only until the first refresh replaces
it. load order is therefore database first, environment second.

And the corollary, in case anyone is tempted: the token does not go in the
`memory` table. That table is injected verbatim into the model's system prompt
on every message.

One more consequence of rotation: two processes refreshing at once will each
get a token and each store one, and the loser's is the one production is left
holding. Here that risk is small (one web process, one heartbeat, and the
heartbeat does not touch Graph), but if To Do is ever called from the tick this
is the thing to fix first.

Environment:
    MS_CLIENT_ID       - Application (client) ID from the Entra app registration
    MS_CLIENT_SECRET   - a client secret VALUE (not its ID) from that app
    MS_REFRESH_TOKEN   - the seed token, from scripts/authorize_microsoft.py
    MS_TENANT          - optional; "common" (default) accepts both a work
                         account and a personal Microsoft account
"""

import logging
import os
import time

import microsoft_scopes
import storage

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
PROVIDER = "microsoft"

# "common" so the same registration serves his personal Microsoft account and a
# work one. A tenant GUID here would lock it to one company.
TENANT = os.environ.get("MS_TENANT", "common").strip() or "common"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"

SCOPES = microsoft_scopes.SCOPES

# Access tokens last an hour. Refreshing 5 minutes early costs one extra round
# trip a day and removes the class of failure where a token expires between the
# check and the call.
EXPIRY_MARGIN_SECONDS = 300

_access_token = ""
_expires_at = 0.0


class GraphError(RuntimeError):
    """A Graph call that came back as an error, carrying its HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def client_id() -> str:
    return os.environ.get("MS_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.environ.get("MS_CLIENT_SECRET", "").strip()


def refresh_token() -> str:
    """The live refresh token: whatever the last rotation stored, else the seed."""
    return storage.load_token(PROVIDER) or os.environ.get("MS_REFRESH_TOKEN", "").strip()


def configured() -> bool:
    """Whether the connection has everything it needs to be attempted at all.

    The tools call this first so an unconfigured assistant says "not connected
    yet" instead of raising, which the model would report to Itai as a fault.
    """
    return bool(client_id() and client_secret() and refresh_token())


def reset() -> None:
    """Drops the cached access token. Used by tests, and after a 401."""
    global _access_token, _expires_at
    _access_token = ""
    _expires_at = 0.0


def _post_token(payload: dict) -> dict:
    import requests

    response = requests.post(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = {}
    try:
        body = response.json()
    except Exception:
        pass
    if response.status_code >= 400:
        # Microsoft puts the useful part in error_description, and the useful
        # part is usually the difference between "re-consent" and "retry".
        detail = body.get("error_description") or response.text[:400]
        raise GraphError(response.status_code, f"Token refresh failed: {detail}")
    return body


def access_token() -> str:
    """A valid access token, refreshing and re-storing the rotated one as needed."""
    global _access_token, _expires_at
    if _access_token and time.time() < _expires_at:
        return _access_token

    if not configured():
        raise GraphError(0, "MS_CLIENT_ID / MS_CLIENT_SECRET / MS_REFRESH_TOKEN are not set")

    body = _post_token({
        "client_id": client_id(),
        "client_secret": client_secret(),
        "refresh_token": refresh_token(),
        "grant_type": "refresh_token",
        # Asked for again on every refresh: Microsoft issues the access token
        # for the scopes named here, not for everything the consent covered, so
        # omitting one silently drops the capability rather than failing.
        "scope": " ".join(SCOPES),
    })

    token = body.get("access_token", "")
    if not token:
        raise GraphError(0, f"No access token in the refresh response: {body}")

    rotated = body.get("refresh_token", "")
    if rotated and rotated != refresh_token():
        if not storage.save_token(PROVIDER, rotated):
            # Not fatal - the token we just used still works for now - but it
            # is the exact shape of the failure described at the top of this
            # file, so it is logged loudly rather than passed over.
            logger.error(
                "Microsoft rotated the refresh token and it could NOT be stored. "
                "The connection will break when the current token is revoked."
            )

    _access_token = token
    _expires_at = time.time() + max(int(body.get("expires_in", 3600)) - EXPIRY_MARGIN_SECONDS, 60)
    return _access_token


def graph(method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
    """One Graph call. `path` is relative to /v1.0, e.g. "/me/todo/lists".

    Returns the parsed body, or {} for the empty 204 that DELETE and PATCH-less
    completions come back with. Raises GraphError on anything Graph rejected.
    """
    import requests

    url = GRAPH_ROOT + path if path.startswith("/") else f"{GRAPH_ROOT}/{path}"

    def call(token: str):
        return requests.request(
            method.upper(),
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=json_body,
            params=params,
            timeout=30,
        )

    response = call(access_token())
    if response.status_code == 401:
        # The cached token was rejected - it may have been revoked rather than
        # merely expired. Throw it away and try once with a genuinely new one
        # before deciding the connection is broken.
        reset()
        response = call(access_token())

    if response.status_code >= 400:
        detail = response.text[:400]
        try:
            detail = response.json()["error"]["message"]
        except Exception:
            pass
        raise GraphError(response.status_code, detail)

    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def get_all(path: str, params: dict | None = None, max_pages: int = 10) -> list:
    """Every page of a Graph collection, flattened.

    Graph pages at 20 items or so by default and hides the rest behind
    @odata.nextLink. A single page looks like a complete answer, which is how an
    assistant confidently tells someone a task is not on a list it only read a
    fifth of.
    """
    import requests

    items = []
    body = graph("GET", path, params=params)
    for _ in range(max_pages):
        items.extend(body.get("value", []) or [])
        next_link = body.get("@odata.nextLink")
        if not next_link:
            break
        response = requests.get(
            next_link,
            headers={"Authorization": f"Bearer {access_token()}"},
            timeout=30,
        )
        if response.status_code >= 400:
            break
        body = response.json()
    return items


def account() -> str:
    """Which Microsoft account the token belongs to - for telling Itai that the
    assistant is holding the wrong one, which is otherwise invisible."""
    me = graph("GET", "/me")
    return me.get("userPrincipalName") or me.get("mail") or me.get("displayName") or "?"


__all__ = [
    "GraphError",
    "SCOPES",
    "account",
    "access_token",
    "configured",
    "get_all",
    "graph",
    "reset",
    "refresh_token",
]
