import json
import logging
import os

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Every message re-sends the whole conversation to Gemini, so unbounded history
# means unbounded cost and, eventually, a context-length failure. 40 entries is
# roughly 20 exchanges - enough for a working day of context.
MAX_HISTORY_ENTRIES = 40


def _parts(entry) -> list:
    """Stored entries are plain dicts (JSONB), but tolerate anything shaped oddly."""
    if not isinstance(entry, dict):
        return []
    parts = entry.get("parts")
    return parts if isinstance(parts, list) else []


def _has(entry, *keys) -> bool:
    return any(
        isinstance(part, dict) and any(part.get(k) is not None for k in keys)
        for part in _parts(entry)
    )


def _is_call(entry) -> bool:
    return _has(entry, "function_call", "functionCall")


def _is_response(entry) -> bool:
    return _has(entry, "function_response", "functionResponse")


def _is_plain_user(entry) -> bool:
    """A turn Itai actually typed - the only thing a conversation may open with,
    and, alongside a function response, the only thing a call may follow."""
    return (
        isinstance(entry, dict)
        and entry.get("role") == "user"
        and not _is_response(entry)
    )


def repair_history(history: list) -> list:
    """Enforces the two turn-ordering rules Gemini rejects a whole request over.

    A function response must directly follow its call, and a call must directly
    follow a real user turn or a function response. Break either and the API
    returns 400 INVALID_ARGUMENT - and because the offending shape is what we
    stored, every later message replays it and fails too. The conversation is
    dead, not degraded.

    Trimming to the last N entries is what creates both violations: the cut can
    land between a call and its response, leaving the response orphaned at the
    head - and dropping that orphan promotes the call behind it to the head,
    where nothing precedes it at all. Fixing only the first half is what took
    production down a second time on 2026-09-06, a few minutes after the first
    fix deployed, so both rules are now enforced against the same walk.

    Nothing is dropped that the API would have accepted: a conversation opening
    on a model turn is fine, and only a call in a position the second rule
    forbids is removed. Being stricter than the API would cost Itai the last
    answer he was given, for no gain.
    """
    if not isinstance(history, list):
        return []

    clean = []
    response_allowed = False
    for entry in history:
        if _is_response(entry):
            if not response_allowed:
                continue
            clean.append(entry)
            # Deliberately leaves response_allowed set: one model turn may emit
            # several calls, answered by several response turns in a row.
            continue
        if _is_call(entry):
            if not clean or not (_is_plain_user(clean[-1]) or _is_response(clean[-1])):
                continue
            clean.append(entry)
            response_allowed = True
            continue
        clean.append(entry)
        response_allowed = False

    while clean and _is_call(clean[-1]):
        clean.pop()

    return clean


_schema_ready = False


def enabled() -> bool:
    """False when no database is configured - the caller then falls back to
    process memory and a local file, which do not survive a restart."""
    return bool(DATABASE_URL)


def _connect():
    import psycopg

    return psycopg.connect(DATABASE_URL)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id  TEXT PRIMARY KEY,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                sender_id  TEXT PRIMARY KEY,
                history    JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sender_state (
                sender_id       TEXT PRIMARY KEY,
                summary         TEXT NOT NULL DEFAULT '',
                pin             JSONB NOT NULL DEFAULT '{}'::jsonb,
                compacted_count INT NOT NULL DEFAULT 0,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sender_topics (
                sender_id  TEXT NOT NULL,
                topic      TEXT NOT NULL,
                summary    TEXT NOT NULL DEFAULT '',
                turns      INT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (sender_id, topic)
            )
        """)
        # One row per conversation turn: the retrieval substrate. topics are
        # persisted per turn so the taxonomy is data (re-tagging can run
        # offline), and to_tsvector over text gives real full-text retrieval
        # instead of Python word overlap. Derivable from conversations -
        # wiping it only costs a re-index.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS turn_index (
                sender_id  TEXT NOT NULL,
                idx        INT  NOT NULL,
                speaker    TEXT NOT NULL DEFAULT '',
                text       TEXT NOT NULL DEFAULT '',
                topics     TEXT[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (sender_id, idx)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                category   TEXT NOT NULL DEFAULT 'general',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Every message the assistant sends on its own initiative is recorded
        # here before it goes out, keyed by a fingerprint the routine builds
        # from the thing being raised (a date for a shift reminder, a message
        # id for an email). The primary key is the whole mechanism: a second
        # attempt to raise the same thing fails to insert and is dropped.
        # Without it the heartbeat repeats itself every few minutes, which is
        # how a proactive assistant turns into a broken one.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS proactive_log (
                fingerprint TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # WhatsApp only allows a free-form business message within 24 hours of
        # the person's own last message, and that clock is what decides whether
        # a reminder can be delivered at all. conversations.updated_at cannot
        # answer it - it moves when the assistant writes back too - so the
        # moment Itai wrote gets its own column.
        cur.execute(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ"
        )
        # A reminder Itai asked for out loud. status is what makes the delivery
        # exactly-once: the heartbeat flips 'pending' to 'sent' in the same
        # statement that selects the row, so two overlapping ticks cannot both
        # win it. A repeating reminder is not a new row - it is this row moved
        # forward and set back to pending, so cancelling it cancels the series.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id         SERIAL PRIMARY KEY,
                sender_id  TEXT NOT NULL,
                text       TEXT NOT NULL,
                due_at     TIMESTAMPTZ NOT NULL,
                recurrence TEXT NOT NULL DEFAULT 'once',
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS reminders_due ON reminders (status, due_at)"
        )
        # A refresh token the assistant holds on Itai's behalf for a provider
        # that rotates it. Microsoft is the reason this table exists: it hands
        # back a NEW refresh token every time the old one is redeemed, so the
        # value in an environment variable is stale from the first refresh
        # onwards and cannot be the live copy. The env var is only ever a seed.
        #
        # Deliberately NOT the memory table. Everything in `memory` is rendered
        # into the model's system instruction on every single message by
        # assistant._load_memory_context - a credential there would be shown to
        # the model, and through it to whoever is talking to the model, on every
        # turn. Nothing secret goes in that table, ever.
        # The investment portfolio Itai handed over once (the holdings export
        # from his broker, forwarded on WhatsApp). One row, one JSON document:
        # the evening summary prices it every day, and a newer export replaces
        # it whole. Deliberately NOT the memory table - holdings are data a
        # routine reads, not facts to render into every prompt.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                holdings    JSONB NOT NULL,
                source      TEXT NOT NULL DEFAULT '',
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider      TEXT PRIMARY KEY,
                refresh_token TEXT NOT NULL,
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()
    _schema_ready = True


def message_seen(message_id: str) -> bool:
    """True when this WhatsApp message id already reached the handler.

    Meta retries a webhook delivery until it gets a fast 200, so a slow
    answer arrives again and again. A check that ERRORS returns False: a
    duplicate answer is bad, a message never answered is worse.
    """
    if not enabled() or not message_id:
        return False
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s",
                            (message_id,))
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Failed to check processed message: {e}")
        return False


def mark_message(message_id: str) -> None:
    """Records a WhatsApp message id as received, before handling starts."""
    if not enabled() or not message_id:
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO processed_messages (message_id) VALUES (%s) "
                    "ON CONFLICT (message_id) DO NOTHING",
                    (message_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to mark processed message: {e}")


def load_history(sender_id: str) -> list:
    """Returns the stored conversation for this sender as plain dicts, oldest first."""
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT history FROM conversations WHERE sender_id = %s", (sender_id,))
                row = cur.fetchone()
        # Repaired on the way out too, so a row already broken by an older trim
        # heals itself on the next message instead of needing a manual edit.
        return repair_history(row[0]) if row else []
    except Exception as e:
        logger.error(f"Failed to load history for {sender_id}: {e}")
        return []


def save_history(sender_id: str, history: list) -> None:
    trimmed = repair_history(history[-MAX_HISTORY_ENTRIES:])
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (sender_id, history, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (sender_id)
                    DO UPDATE SET history = EXCLUDED.history, updated_at = now()
                    """,
                    (sender_id, json.dumps(trimmed, ensure_ascii=False)),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save history for {sender_id}: {e}")


def load_memory() -> dict:
    """Returns all long-term facts as {key: {"value": ..., "category": ...}}."""
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT key, value, category FROM memory")
                rows = cur.fetchall()
        return {k: {"value": v, "category": c} for k, v, c in rows}
    except Exception as e:
        logger.error(f"Failed to load memory: {e}")
        return {}


def save_memory(key: str, value: str, category: str = "general") -> None:
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory (key, value, category, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value,
                                  category = EXCLUDED.category,
                                  updated_at = now()
                    """,
                    (key, value, category),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save memory item {key}: {e}")
        raise


def load_portfolio() -> list:
    """The stored holdings as a list of dicts, [] when none was ever saved."""
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT holdings FROM portfolio WHERE id = 1")
                row = cur.fetchone()
        if not row:
            return []
        holdings = row[0]
        return holdings if isinstance(holdings, list) else []
    except Exception as e:
        logger.error(f"Failed to load portfolio: {e}")
        return []


def save_portfolio(holdings: list, source: str = "") -> bool:
    """Replaces the stored portfolio with a fresh export. False when no
    database is configured - the caller then says it could not keep it."""
    if not enabled():
        logger.error("save_portfolio called without a database")
        return False
    import json as _json
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO portfolio (id, holdings, source, updated_at)
                    VALUES (1, %s::jsonb, %s, now())
                    ON CONFLICT (id)
                    DO UPDATE SET holdings = EXCLUDED.holdings,
                                  source = EXCLUDED.source,
                                  updated_at = now()
                    """,
                    (_json.dumps(holdings, ensure_ascii=False), source or ""),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save portfolio: {e}")
        return False


# --- rotating OAuth refresh tokens --------------------------------------
#
# See the oauth_tokens comment in _ensure_schema for why these are not stored
# in `memory` and must never be moved there.


def load_token(provider: str) -> str:
    """The live refresh token for a provider, or "" when there is none stored.

    Fails soft on purpose: a database that is down should make the connection
    fall back to its environment-variable seed and log, not raise inside a tool
    call the model is waiting on.
    """
    if not enabled():
        return ""
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT refresh_token FROM oauth_tokens WHERE provider = %s",
                    (provider,),
                )
                row = cur.fetchone()
        return row[0] if row else ""
    except Exception as e:
        logger.error(f"Failed to load refresh token for {provider}: {e}")
        return ""


def save_token(provider: str, refresh_token: str) -> bool:
    """Records the newest refresh token for a provider. Returns whether it stuck.

    The caller needs the answer rather than an exception: with Microsoft, a
    token that was redeemed but not stored is a token that will be lost the
    moment the previous one is finally revoked, and the only useful response is
    to say so out loud while the old one still works.
    """
    if not (enabled() and refresh_token):
        return False
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO oauth_tokens (provider, refresh_token, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (provider)
                    DO UPDATE SET refresh_token = EXCLUDED.refresh_token,
                                  updated_at = now()
                    """,
                    (provider, refresh_token),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save refresh token for {provider}: {e}")
        return False


# --- the proactive side -------------------------------------------------
#
# Everything below is read and written by the heartbeat rather than by a
# conversation, so all of it fails soft: a database that is down must make the
# assistant quiet, never make it crash the endpoint that woke it.


def claim(fingerprint: str, kind: str) -> bool:
    """Reserves the right to send one proactive message, exactly once, ever.

    Returns True only for the caller that inserted the row. The claim happens
    BEFORE the message is sent, so two overlapping ticks cannot both decide to
    remind Itai of the same shift; if the send then fails, the caller calls
    release() to put it back. Any database error returns False - not sending is
    the safe direction when we cannot tell whether we already sent.
    """
    if not enabled():
        # No ledger means no way to promise "only once", and sending twice is
        # worse than not sending. Silence is the safe failure here.
        return False
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO proactive_log (fingerprint, kind)
                    VALUES (%s, %s)
                    ON CONFLICT (fingerprint) DO NOTHING
                    """,
                    (fingerprint, kind),
                )
                claimed = cur.rowcount == 1
            conn.commit()
        return claimed
    except Exception as e:
        logger.error(f"Failed to claim proactive item {fingerprint}: {e}")
        return False


def restamp(fingerprint: str, kind: str) -> None:
    """Changes what an existing claim is recorded as, keeping the claim itself.

    The mail watch claims an email before it knows what the email is, so the
    row starts life as a plain 'mail'. When triage decides nobody should be
    told about it, the claim must stay - that is what stops the same newsletter
    being examined again on every tick - but the row should say what actually
    happened to it, so the silenced mail is auditable rather than invisible.
    """
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE proactive_log SET kind = %s WHERE fingerprint = %s",
                    (kind, fingerprint),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to restamp proactive item {fingerprint}: {e}")


def release(fingerprint: str) -> None:
    """Undoes a claim whose message never actually went out, so the next tick
    may try again while the item is still worth raising."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM proactive_log WHERE fingerprint = %s", (fingerprint,))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to release proactive item {fingerprint}: {e}")


def prune_proactive_log(days: int = 60) -> None:
    """The ledger only has to remember long enough to stop a repeat. Rows older
    than that are dead weight on a free database with a 1GB ceiling."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM proactive_log WHERE sent_at < now() - make_interval(days => %s)",
                    (days,),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to prune the proactive log: {e}")


def note_inbound(sender_id: str) -> None:
    """Records that Itai wrote, which is what reopens the 24-hour window.

    Called for every incoming message, including ones the assistant cannot
    answer - an unsupported voice note still reopens the window as far as
    WhatsApp is concerned.
    """
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (sender_id, history, last_inbound_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (sender_id) DO UPDATE SET last_inbound_at = now()
                    """,
                    (sender_id, json.dumps([])),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to note an inbound message from {sender_id}: {e}")


def last_inbound():
    """Returns (sender_id, last_inbound_at) for whoever wrote most recently, or
    (None, None). This is how a tick finds Itai's number without one being
    configured, and how it knows whether the free window is still open."""
    if not enabled():
        return (None, None)
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sender_id, last_inbound_at FROM conversations
                    WHERE last_inbound_at IS NOT NULL
                    ORDER BY last_inbound_at DESC LIMIT 1
                    """
                )
                row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        logger.error(f"Failed to read the last inbound message: {e}")
        return (None, None)


def append_model_turn(sender_id: str, text: str) -> None:
    """Writes a message the assistant sent on its own into the conversation.

    A proactive message is a turn Itai can reply to - "כן, תקרא" only means
    anything if the model can see what it just said. Without this the model
    receives an answer to a question it has no record of asking.
    """
    if not enabled():
        return
    history = load_history(sender_id)
    history.append({"role": "model", "parts": [{"text": text}]})
    save_history(sender_id, history)


def append_user_turn(sender_id: str, text: str) -> None:
    """Writes a message Itai sent into the conversation.

    The normal path never needs this - the Gemini SDK records the user turn as
    part of sending it. It is needed only when the reply came from a fallback
    provider that knows nothing about our history, where without it the stored
    conversation would show an answer to a question nobody asked.
    """
    if not enabled():
        return
    history = load_history(sender_id)
    history.append({"role": "user", "parts": [{"text": text}]})
    save_history(sender_id, history)


# --- reminders ----------------------------------------------------------
#
# Same failure posture as the rest of the proactive side: every one of these
# swallows its errors, because they run inside the heartbeat. The one that does
# not is add_reminder, which runs inside a conversation - there, silently
# failing to save would have Itai believe he has a reminder that does not
# exist, which is the worst outcome available.


def add_reminder(sender_id: str, text: str, due_at, recurrence: str = "once") -> int | None:
    """Stores one reminder and returns its id, or None if it could not be stored."""
    if not enabled():
        return None
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders (sender_id, text, due_at, recurrence)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (sender_id, text, due_at, recurrence),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id
    except Exception as e:
        logger.error(f"Failed to store a reminder for {sender_id}: {e}")
        return None


def claim_due_reminders(now, limit: int = 5) -> list:
    """Takes ownership of every reminder that has come due, and returns them.

    Selecting and flipping the status in one statement is the whole guarantee.
    Reading the due rows and updating them afterwards leaves a gap in which a
    second tick reads the same rows, and the result is a reminder delivered
    twice - which on WhatsApp reads as a broken assistant.

    The limit is a burst guard: an instance asleep over a weekend can come back
    to a pile of due reminders, and sending fifteen messages at once is not
    catching up, it is spamming. The rest stay pending for the next tick.
    """
    if not enabled():
        return []
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE reminders SET status = 'sent'
                    WHERE id IN (
                        SELECT id FROM reminders
                        WHERE status = 'pending' AND due_at <= %s
                        ORDER BY due_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, sender_id, text, due_at, recurrence
                    """,
                    (now, limit),
                )
                rows = cur.fetchall()
            conn.commit()
        return [
            {"id": r[0], "sender_id": r[1], "text": r[2], "due_at": r[3], "recurrence": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Failed to claim due reminders: {e}")
        return []


def unclaim_reminder(reminder_id: int) -> None:
    """Puts a claimed reminder back when its message never went out."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reminders SET status = 'pending' WHERE id = %s", (reminder_id,)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to unclaim reminder {reminder_id}: {e}")


def reschedule_reminder(reminder_id: int, next_due) -> None:
    """Moves a repeating reminder to its next occurrence and arms it again."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reminders SET due_at = %s, status = 'pending' WHERE id = %s",
                    (next_due, reminder_id),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to reschedule reminder {reminder_id}: {e}")


def open_reminders(sender_id: str, limit: int = 20) -> list:
    """Everything still waiting to fire, soonest first."""
    if not enabled():
        return []
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, text, due_at, recurrence FROM reminders
                    WHERE sender_id = %s AND status = 'pending'
                    ORDER BY due_at LIMIT %s
                    """,
                    (sender_id, limit),
                )
                rows = cur.fetchall()
        return [{"id": r[0], "text": r[1], "due_at": r[2], "recurrence": r[3]} for r in rows]
    except Exception as e:
        logger.error(f"Failed to list reminders for {sender_id}: {e}")
        return []


def cancel_reminder(reminder_id: int, sender_id: str) -> bool:
    """Drops a reminder, series and all. Scoped to the sender so one person's
    reminder id can never delete somebody else's row."""
    if not enabled():
        return False
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM reminders WHERE id = %s AND sender_id = %s",
                    (reminder_id, sender_id),
                )
                removed = cur.rowcount == 1
            conn.commit()
        return removed
    except Exception as e:
        logger.error(f"Failed to cancel reminder {reminder_id}: {e}")
        return False


def load_state(sender_id: str) -> dict:
    """The compaction state for one conversation: rolling summary, pinned
    facts, and how many entries the summary has absorbed so far. Anything
    failure-shaped returns the empty state - a missing summary is a longer
    prompt, never a wrong one."""
    empty = {"summary": "", "pin": {}, "compacted_count": 0}
    if not enabled():
        return empty
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT summary, pin, compacted_count FROM sender_state WHERE sender_id = %s",
                    (sender_id,))
                row = cur.fetchone()
        if not row:
            return empty
        return {"summary": row[0] or "", "pin": row[1] or {}, "compacted_count": row[2] or 0}
    except Exception as e:
        logger.error(f"Failed to load state for {sender_id}: {e}")
        return empty


def save_state(sender_id: str, summary=None, pin=None, compacted_count=None) -> None:
    if not enabled():
        return
    try:
        current = load_state(sender_id)
        summary = current["summary"] if summary is None else summary
        pin = current["pin"] if pin is None else pin
        compacted_count = current["compacted_count"] if compacted_count is None else compacted_count
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sender_state (sender_id, summary, pin, compacted_count, updated_at)
                    VALUES (%s, %s, %s, now(), %s)
                    ON CONFLICT (sender_id) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        pin = EXCLUDED.pin,
                        compacted_count = EXCLUDED.compacted_count,
                        updated_at = now()
                    """,
                    (sender_id, summary, json.dumps(pin, ensure_ascii=False), compacted_count),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save state for {sender_id}: {e}")


def pin_fact(sender_id: str, key: str, value: str) -> None:
    """One fact that must survive every compaction verbatim: a pending plan,
    an approval he gave, an id a later step depends on. Pins render into every
    context bundle ahead of everything else and are never summarised.

    Corrections supersede by key: writing the same key again overwrites the
    old claim and stamps it, so a changed phone number or price never lives
    on as a stale duplicate. Entries are stored as {value, updated_at}; a
    bare string is a legacy pin from before timestamps and reads as value."""
    import datetime
    state = load_state(sender_id)
    pin = dict(state["pin"])
    pin[key] = {"value": value,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save_state(sender_id, pin=pin)


def unpin_fact(sender_id: str, key: str) -> None:
    state = load_state(sender_id)
    pin = dict(state["pin"])
    if key in pin:
        del pin[key]
        save_state(sender_id, pin=pin)


def load_topics(sender_id: str) -> dict:
    """Every topic's rolling summary for one conversation: {topic: summary}."""
    if not enabled():
        return {}
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic, summary FROM sender_topics WHERE sender_id = %s",
                    (sender_id,))
                return {t: s or "" for t, s in cur.fetchall()}
    except Exception as e:
        logger.error(f"Failed to load topics for {sender_id}: {e}")
        return {}


def save_topic(sender_id: str, topic: str, summary: str, added_turns: int) -> None:
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sender_topics (sender_id, topic, summary, turns, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (sender_id, topic) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        turns = sender_topics.turns + EXCLUDED.turns,
                        updated_at = now()
                    """,
                    (sender_id, topic, summary, added_turns),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save topic {topic} for {sender_id}: {e}")


def indexed_turn_count(sender_id: str) -> int:
    if not enabled():
        return 0
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM turn_index WHERE sender_id = %s", (sender_id,))
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Failed to count indexed turns for {sender_id}: {e}")
        return 0


def index_turn(sender_id: str, idx: int, speaker: str, text: str, topics) -> None:
    """Append one turn to the retrieval index. Idempotent on (sender, idx)."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO turn_index (sender_id, idx, speaker, text, topics)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (sender_id, idx) DO NOTHING
                    """,
                    (sender_id, idx, speaker, text, list(topics)),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to index turn {idx} for {sender_id}: {e}")


def clear_turn_index(sender_id: str) -> None:
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM turn_index WHERE sender_id = %s", (sender_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to clear turn index for {sender_id}: {e}")


def clear_topics(sender_id: str) -> None:
    """Drop every topic digest. Digests are derivable from the raw history,
    so this only forces a rebuild - nothing irreplaceable is lost."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sender_topics WHERE sender_id = %s", (sender_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to clear topics for {sender_id}: {e}")


def search_turns(sender_id: str, query: str, limit: int = 6) -> list:
    """Full-text retrieval over indexed turns. Postgres tsvector ('simple'
    config - language-neutral, so Hebrew text tokenises without a dictionary).
    Any failure returns [] and the caller falls back to the Python scorer."""
    if not enabled() or not (query or "").strip():
        return []
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT speaker, text,
                           ts_rank(to_tsvector('simple', text),
                                   plainto_tsquery('simple', %s)) AS rank
                    FROM turn_index
                    WHERE sender_id = %s
                      AND to_tsvector('simple', text) @@ plainto_tsquery('simple', %s)
                    ORDER BY rank DESC, idx DESC
                    LIMIT %s
                    """,
                    (query, sender_id, query, limit),
                )
                return [f"{sp}: {tx}" for sp, tx, _ in cur.fetchall()]
    except Exception as e:
        logger.error(f"Full-text search failed for {sender_id}: {e}")
        return []
