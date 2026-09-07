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
            CREATE TABLE IF NOT EXISTS conversations (
                sender_id  TEXT PRIMARY KEY,
                history    JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    conn.commit()
    _schema_ready = True


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
