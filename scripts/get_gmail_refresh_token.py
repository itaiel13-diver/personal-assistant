"""One-time (or, while the OAuth app is in "Testing", weekly) helper that turns a
Google OAuth client into a refresh token for the assistant's Gmail and Drive
access.

Why this exists: neither Gmail nor "files shared with me" in Drive has a
service-account path for a personal @gmail.com account, so access requires a
human to approve once in a browser. This script does everything around that
approval.

One consent, one token, every scope. Google issues a single refresh token per
consent and each new consent supersedes the last, so approving one API on its
own silently revokes the rest - running this without Drive in the list is how
you take Drive away. The list is therefore not kept here at all: it is
google_scopes.SCOPES, the same object drive_tools and gmail_tools present back
to Google, so the two cannot drift.

Usage:
    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python scripts/get_gmail_refresh_token.py

It prints a URL. Open it, approve as the WORK account, and Google will redirect
to a localhost address that fails to load - that is expected. Copy the "code"
parameter out of the browser's address bar and paste it back here.

The refresh token it prints goes into BOTH GMAIL_REFRESH_TOKEN and
GOOGLE_REFRESH_TOKEN in the environment. gmail_tools reads only the first name
and drive_tools prefers the second, so setting one of the two leaves half the
assistant looking at a token that is no longer valid.

One thing to read in the response it prints: refresh_token_expires_in. If that
field is there at all, the OAuth consent screen is still in "Testing" and the
token dies in seven days no matter what else is done. The fix is PUBLISH APP on
the OAuth consent screen - not verification, which a personal app under 100
users does not need - and then a fresh run of this script.
"""

import os
import sys
import urllib.parse
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import google_scopes

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
# A Desktop-app client accepts this redirect; nothing actually listens on it.
REDIRECT_URI = "http://localhost:8080/"
# The same list drive_tools and gmail_tools present, by reference rather than by
# a comment asking the next person to keep three copies in step. What is in it
# and why is documented in google_scopes.
SCOPES = google_scopes.SCOPES


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
    # Both names, because gmail_tools reads only the first and drive_tools
    # prefers the second: setting one leaves half the assistant on a token that
    # this consent has just invalidated.
    print("\nGMAIL_REFRESH_TOKEN=" + tokens["refresh_token"])
    print("GOOGLE_REFRESH_TOKEN=" + tokens["refresh_token"])
    if "refresh_token_expires_in" in tokens:
        print(
            "\nWARNING: this token expires in "
            f"{int(tokens['refresh_token_expires_in']) // 86400} days. That field is "
            "only present while the OAuth consent screen is in 'Testing'. Publish "
            "the app (PUBLISH APP on the consent screen - verification is not "
            "needed under 100 users) and run this again for a token that lasts."
        )
