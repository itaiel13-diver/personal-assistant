"""The one list of Google scopes this assistant's token carries.

It lives in its own module because three places need to agree on it and used to
agree only by a comment saying "must stay identical": the refresh-token script
that asks Google for consent, and the two modules that present the resulting
token back to Google. When those drifted, nothing failed at import time - it
failed in production as an opaque 403 on a call that had worked yesterday.

Why it is this wide. Itai's standing instruction, given on 2026-09-07 and
written down here so it is not re-litigated every time: when he asks for a
connection he wants the whole capability handed over at once, and would rather
narrow it afterwards if something goes wrong than keep discovering a missing
verb mid-task. Each re-consent also costs him a browser round trip and, worse,
supersedes the token in production the moment he finishes it - so a scope left
out today is an outage tomorrow.

What that means for mail, stated plainly. https://mail.google.com/ replaced the
old gmail.readonly + gmail.compose pair, and it permits sending and permanently
deleting mail. The old pair did too, in fact - gmail.compose was verified
against the live API to send a draft - so this is a difference of degree, not of
kind. Either way the token has never been what stops the assistant sending
mail. That guarantee is entirely in gmail_tools: it exposes no sending function
and calls send() nowhere, and tests/test_gmail_tools.py reads its syntax tree to
keep it that way. Widening the scope makes that guard more load-bearing, not
less, so do not relax it to make a feature fit.
"""

SCOPES = [
    # Mail: read, draft, and everything else the mailbox allows. See above for
    # why the narrow pair was dropped and what actually holds the line.
    "https://mail.google.com/",
    # Drive: the full scope rather than drive.file, because drive.file is blind
    # to every file somebody shared with Itai - which is most of what he asks
    # about. The restraint lives in drive_tools, not in the scope.
    "https://www.googleapis.com/auth/drive",
    # The three editors, so a Doc, a Sheet or a Deck can be read and written as
    # itself rather than only as an exported blob of text.
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    # Calendar and Tasks. calendar_tools still reaches the calendar through a
    # service account; this is here so moving it onto the user token later does
    # not need another consent from Itai.
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    # Contacts, so "email Nikita" can resolve a name to an address. Note that
    # this alone does not grant people/me - that self-lookup wants the separate
    # profile scope, and its absence is why a people/me probe returns 403 while
    # people/me/connections is fine.
    "https://www.googleapis.com/auth/contacts",
]
