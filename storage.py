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
