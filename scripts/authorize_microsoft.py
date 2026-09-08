"""One-time helper that turns an Entra app registration into a Microsoft
refresh token for the assistant's To Do access.

Why a browser is involved at all: Microsoft Graph has no application-permission
path for To Do. The whole Tasks.* family is delegated-only, so there is no
service-account equivalent and no way to skip the human. Itai approves once,
here, and the assistant then acts as him.

Usage (on a machine with a browser, or paste the URL into one):
    MS_CLIENT_ID=... MS_CLIENT_SECRET=... python scripts/authorize_microsoft.py

It prints a URL. Open it, sign in as the Microsoft account whose To Do lists
matter, approve, and Microsoft redirects to a localhost address that fails to
load - that is expected, nothing is listening there. Copy the "code" parameter
out of the browser's address bar and paste it back.

What to do with what it prints: put it in MS_REFRESH_TOKEN. Note the word SEED
in msgraph's docstring - Microsoft replaces the refresh token on every use, so
this value is only what the assistant starts from. From the first refresh
onwards the live token lives in the oauth_tokens table, and re-running this
script is only needed if that stored token is lost or the consent is revoked.

Two things that go wrong here and look like a bug in the script:

  AADSTS65001 / "need admin approval" - the account is a work account whose
  tenant blocks user consent. Tasks.ReadWrite is normally user-consentable, but
  a tenant can turn that off for every app. A personal Microsoft account has no
  such switch, so try that first.

  AADSTS7000215 "invalid client secret" - the SECRET ID was pasted instead of
  the secret VALUE. Entra shows both, the Value only once, and hides it after a
  page refresh; if it is gone, make a new secret. They also expire - 24 months
  at the outside - and the expiry is silent until a refresh fails.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import microsoft_scopes

CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
# "common" accepts a work account and a personal Microsoft account with the same
# registration. Nothing listens on the redirect; the browser failing to load it
# is the expected end of the flow.
TENANT = os.environ.get("MS_TENANT", "common")
REDIRECT_URI = "http://localhost:8080/"
# By reference, so the consent and the refresh cannot ask for different things.
SCOPES = microsoft_scopes.SCOPES

AUTH_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def auth_url() -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        # Forces the consent screen even on a repeat approval, so the scopes
        # actually get re-granted rather than silently reusing an older set.
        "prompt": "consent",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange(code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": " ".join(SCOPES),
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Microsoft's useful message is in the error body, which urllib would
        # otherwise swallow behind a bare "HTTP Error 400".
        return json.loads(e.read().decode())


if __name__ == "__main__":
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("Set MS_CLIENT_ID and MS_CLIENT_SECRET first.")
    print("\n1. Open this URL and approve as the right Microsoft account:\n")
    print(auth_url())
    print("\n2. Microsoft will redirect to a localhost page that does not load.")
    print("   Copy the value of 'code=' from the address bar.\n")
    code = input("Paste the code here: ").strip()
    tokens = exchange(code)
    if "refresh_token" not in tokens:
        sys.exit(f"No refresh token returned. Full response: {tokens}")
    print("\nMS_REFRESH_TOKEN=" + tokens["refresh_token"])
    print("\nScopes actually granted: " + tokens.get("scope", "(none reported)"))
    print(
        "\nThis is a SEED. Microsoft replaces the refresh token on every use, so "
        "after the first refresh the live one is in the oauth_tokens table, not "
        "in this variable."
    )
