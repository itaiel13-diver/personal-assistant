"""One-time (or, while the OAuth app is in "Testing", weekly) helper that turns a
Google OAuth client into a refresh token for the assistant's Gmail and Drive
access.

Why this exists: neither Gmail nor "files shared with me" in Drive has a
service-account path for a personal @gmail.com account, so access requires a
human to approve once in a browser. This script does everything around that
approval.

One token, three scopes. Drive was added on 2026-09-07 and mail's two scopes
stayed, because Google issues one refresh token per consent: approving Drive on
its own would have silently revoked the mail access the assistant already had.
Whoever runs this again must keep all three here.

Usage:
    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python scripts/get_gmail_refresh_token.py

It prints a URL. Open it, approve as the WORK account, and Google will redirect
to a localhost address that fails to load - that is expected. Copy the "code"
parameter out of the browser's address bar and paste it back here.

The refresh token it prints goes into GMAIL_REFRESH_TOKEN in the environment,
and covers Drive as well - drive_tools reads the same variable.
"""

import os
import sys
import urllib.parse
import urllib.request
import json

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
# A Desktop-app client accepts this redirect; nothing actually listens on it.
REDIRECT_URI = "http://localhost:8080/"
# Must stay identical to drive_tools.SCOPES - the token minted here is the one
# that module presents. Why the full drive scope rather than drive.file: the
# narrow one is blind to files the app did not create, which is every file
# somebody shared with Itai. The restraint lives in drive_tools, not in the
# scope. See that module's docstring.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive",
]


def auth_url() -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # Without these two Google returns no refresh token on repeat approvals.
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange(code: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


if __name__ == "__main__":
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first.")
    print("\n1. Open this URL and approve as the work account:\n")
    print(auth_url())
    print("\n2. Google will redirect to a localhost page that does not load.")
    print("   Copy the value of 'code=' from the address bar.\n")
    code = input("Paste the code here: ").strip()
    tokens = exchange(code)
    if "refresh_token" not in tokens:
        sys.exit(f"No refresh token returned. Full response: {tokens}")
    print("\nGMAIL_REFRESH_TOKEN=" + tokens["refresh_token"])
