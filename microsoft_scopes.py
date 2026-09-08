"""The one list of Microsoft Graph scopes this assistant's token carries.

Same reasoning as google_scopes, and the same failure it exists to prevent: the
consent script, the token refresh and the tool module all have to name the same
scopes, and when they drift nothing breaks at import time - it breaks in
production as a 403 on a call that worked yesterday.

Why it is this wide. Itai's standing instruction from 2026-09-07: when he asks
for a connection, hand over the whole capability at once and narrow it later if
something goes wrong, rather than sending him back to a browser every time a
verb turns out to be missing. So this asks for everything Microsoft To Do has
to offer a delegated app.

What "maximum" actually means here, because it is worth writing down once.
Microsoft Graph has no application-permission (daemon) path for To Do at all -
the Tasks.* family is delegated-only, so a human must approve in a browser and
the assistant acts as that human. Within that family these four are the whole
surface:

  Tasks.ReadWrite         - his own lists and tasks, read and write. Implies
                            Tasks.Read, so listing the narrower one adds nothing.
  Tasks.ReadWrite.Shared   - lists other people shared with him, read and write.
                            Without it those lists are invisible, exactly the way
                            drive.file was blind to shared files.
  User.Read                - /me, so the assistant can say whose account it is
                            holding and fail loudly if it is the wrong one.
  offline_access           - the refresh token. Without it the connection lasts
                            one hour and then dies silently.

There is nothing above Tasks.ReadWrite.Shared to ask for. Mail.*, Calendars.*
and Files.* are deliberately absent: Google already serves all three here, and
a second mailbox token is a second thing to leak.
"""

SCOPES = [
    "offline_access",
    "User.Read",
    "Tasks.ReadWrite",
    "Tasks.ReadWrite.Shared",
]
