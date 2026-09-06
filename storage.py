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


def repair_history(history: list) -> list:
    """Drops function-response turns that have no function call in front of them.

    Gemini rejects the whole request with 400 INVALID_ARGUMENT if a function
    response turn does not immediately follow a function call turn - and once
    that history is stored, every later message replays it and fails too, so
    the conversation is dead until the row is repaired. Trimming to the last N
    entries is exactly what creates the orphan: the cut can land between a call
    and its response, leaving the response at the head. A trailing call with no
    response is the mirror image of the same problem and is dropped as well.
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
            # A model turn may emit several calls answered over several turns.
            continue
        clean.append(entry)
        response_allowed = _is_call(entry)

    while clean and _is_call(clean[-1]) and not _is_response(clean[-1]):
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
