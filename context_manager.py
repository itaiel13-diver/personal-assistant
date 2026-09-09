"""One bounded context bundle for every model call, however long the chat runs.

The failure this replaces: every message rehydrated the FULL conversation and
the FULL long-term memory into the prompt. Past a few weeks of chatting that
is thousands of tokens per call - past a busy morning it is over the free
tier's 8K-minute budget by itself, which is how a list question dies of
throttles while the answer sits in the database.

The bundle has four parts, assembled under a hard token budget:

1. pins - pending plans, approvals, ids. Always verbatim, never summarised,
   never dropped. Written by tools, not by the model.
2. summary - a rolling durable digest of everything older than the recent
   window, produced after a turn (not during one) by maybe_compact.
3. recent turns - the last few exchanges verbatim, so follow-ups like
   "כן, תוסיף" resolve without any retrieval at all.
4. retrieved - older turns and memory lines that share words with the new
   message, in budget left after 1-3. This is how "מה מספר החוג של דנה?"
   finds a fact said three weeks ago without carrying three weeks of chat.

Nothing here deletes: the full history stays in the conversations table, the
summary only decides what rides along. A summary that cannot be produced
(quota-dead day) leaves the bundle as recent + retrieved - shorter prompts,
never wrong ones.
"""

import logging
import re

import storage

logger = logging.getLogger(__name__)

# Conservative for Hebrew, where a token is often two characters.
CHARS_PER_TOKEN = 3.0
# The whole bundle - pins, summary, recent, retrieved - must fit in this, so
# the system prompt, the tool schemas and the reply all still fit the free
# tier's per-minute budget on the same call.
CONTEXT_TOKEN_BUDGET = 2200
SUMMARY_MAX_TOKENS = 500
RECENT_TURNS = 6
RETRIEVED_TURNS = 6
RETRIEVED_MEMORY_LINES = 8
# Compaction kicks in once this many entries sit older than the recent window.
COMPACT_THRESHOLD = 24
# Summaries digest this many old entries per pass, so one pass is one small call.
COMPACT_BATCH = 40


# The topic index: lightweight keyword sets, matching how tool packs are
# chosen. A turn can carry several tags - a message about "המשימה ביומן" is
# todo AND calendar - which is exactly why rigid single folders were dropped.
TOPIC_KEYWORDS = {
    "todo": ("משימ", "טודו", "todo", "חנות", "התקנ", "פירוק", "לטפל"),
    "calendar": ("פגיש", "יומן", "אירוע", "מפגש", "לזמן", "שיבוץ", "calendar"),
    "mail": ("מייל", "דואר", "gmail", "e-mail", "email", "מכתב", "טיוט"),
    "drive": ("קובץ", "קבצים", "תיקי", "דרייב", "drive", "מסמך", "שיתוף"),
    "money": ("כסף", "מחיר", "עלה", "שקל", "דולר", "ביטקוין", "מניה", "תיק", "עמלה"),
    "reminders": ("תזכורת", "תזכיר", "remind", "חוג"),
    "family": ("אמא", "אבא", "אח", "אחות", "דנה", "ילד", "משפחה", "חבר"),
    "general": (),
}
# Topic summaries pulled into one bundle: the strongest matches first.
RETRIEVED_TOPICS = 2
TOPIC_SUMMARY_MAX_TOKENS = 350


def tag_turn(text: str) -> set:
    """Every topic a turn belongs to, deterministic and model-free: tagging
    must work on a quota-dead day too. No hit means 'general'."""
    low = (text or "").lower()
    tags = {topic for topic, words in TOPIC_KEYWORDS.items()
            if words and any(w in low for w in words)}
    return tags or {"general"}


def _topic_scores(incoming_text: str, topic_summaries: dict) -> list:
    needles = _words(incoming_text)
    scored = []
    for topic, summary in topic_summaries.items():
        index_words = set(TOPIC_KEYWORDS.get(topic, ()))
        hit = len(needles & {w for w in index_words if len(w) >= 3})
        hit += _score(summary, needles)
        if hit:
            scored.append((hit, topic))
    return [topic for _, topic in sorted(scored, reverse=True)]


def estimate_tokens(text: str) -> int:
    """A cheap upper bound; overestimating is safe here, underestimating is not."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def _turn_texts(history: list) -> list:
    """Stored history entries as (speaker, text), tool noise dropped."""
    turns = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        speaker = "Itai" if entry.get("role") == "user" else "Assistant"
        for part in entry.get("parts") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if text:
                turns.append(f"{speaker}: {text}")
    return turns


def _words(text: str) -> set:
    return {w for w in re.findall(r"[\w]{3,}", (text or "").lower())}


def _score(text: str, needles: set) -> int:
    return len(_words(text) & needles)


def build_context(sender_id: str, incoming_text: str = "") -> dict:
    """The bounded bundle. Always returns, whatever storage looks like."""
    state = storage.load_state(sender_id)
    history = storage.load_history(sender_id)
    turns = _turn_texts(history)
    recent = turns[-RECENT_TURNS:]
    older = turns[:-RECENT_TURNS] if len(turns) > RECENT_TURNS else []

    needles = _words(incoming_text)
    retrieved = sorted((t for t in older if _score(t, needles) > 0),
                       key=lambda t: _score(t, needles), reverse=True)[:RETRIEVED_TURNS]

    memory_lines = []
    try:
        mem = storage.load_memory() if storage.enabled() else {}
    except Exception:
        mem = {}
    for key, fact in (mem or {}).items():
        value = fact.get("value") if isinstance(fact, dict) else str(fact)
        line = f"{key}: {value}"
        if needles and _score(line, needles) == 0:
            continue
        memory_lines.append((-_score(line, needles), line))
    memory = [line for _, line in sorted(memory_lines)[:RETRIEVED_MEMORY_LINES]]

    pin_lines = [f"{k}: {v}" for k, v in (state["pin"] or {}).items()]

    # The topic layer: matched topic digests beat loose turn retrieval, and
    # each is capped so one runaway topic cannot eat the bundle.
    topic_summaries = storage.load_topics(sender_id)
    matched_topics = _topic_scores(incoming_text, topic_summaries)[:RETRIEVED_TOPICS]
    topic_lines = []
    for topic in matched_topics:
        summary = topic_summaries[topic]
        if estimate_tokens(summary) > TOPIC_SUMMARY_MAX_TOKENS:
            summary = summary[: int(TOPIC_SUMMARY_MAX_TOKENS * CHARS_PER_TOKEN)]
        topic_lines.append(f"[נושא: {topic}] {summary}")

    # Budget assembly: pins and summary are promises, recent is continuity;
    # retrieval fills what is left. If recent alone is too fat, its oldest
    # turns go first - retrieval can still find them by content.
    budget = CONTEXT_TOKEN_BUDGET
    used = estimate_tokens("\n".join(pin_lines)) + min(estimate_tokens(state["summary"]), SUMMARY_MAX_TOKENS)
    kept_recent = list(recent)
    while kept_recent and used + estimate_tokens("\n".join(kept_recent)) > budget * 0.7:
        kept_recent.pop(0)
    used += estimate_tokens("\n".join(kept_recent))

    filler, filler_used = [], 0
    for line in topic_lines + [f"[זכרון] {l}" for l in memory] + [f"[קשור] {t}" for t in retrieved]:
        cost = estimate_tokens(line)
        if used + filler_used + cost > budget:
            continue
        filler.append(line)
        filler_used += cost

    return {
        "pin": pin_lines,
        "summary": state["summary"],
        "recent": kept_recent,
        "retrieved": filler,
        "estimated_tokens": used + filler_used,
        "total_turns": len(turns),
    }


def render(bundle: dict) -> str:
    """The bundle as prompt text, each part labelled so the model trusts it."""
    parts = []
    if bundle["pin"]:
        parts.append("[פריטים פתוחים - מדויק, לא לסכם]:\n" + "\n".join(bundle["pin"]))
    if bundle["summary"]:
        parts.append("[סיכום השיחה עד כה]:\n" + bundle["summary"])
    if bundle["retrieved"]:
        parts.append("[פניני עבר רלוונטיים]:\n" + "\n".join(bundle["retrieved"]))
    if bundle["recent"]:
        parts.append("[התורנויות האחרונות]:\n" + "\n".join(bundle["recent"]))
    return "\n\n".join(parts)


def maybe_compact(sender_id: str, summarize_fn) -> bool:
    """Folds the oldest turns into the durable summary, after a turn, never
    during one. summarize_fn(existing_summary, turns_text) -> new summary; it
    is injected so a quota-dead day simply skips compaction - the bundle
    stays bounded either way, this only decides how much it remembers by
    digest versus by retrieval. Pins are not part of the digest, ever.
    """
    history = storage.load_history(sender_id)
    turns = _turn_texts(history)
    overflow = len(turns) - RECENT_TURNS
    if overflow < COMPACT_THRESHOLD:
        return False
    state = storage.load_state(sender_id)
    batch = turns[:COMPACT_BATCH]
    # Multi-tag: one turn feeds the digest of EVERY topic it touches, so a
    # question later finds it under any of them. One small call per topic.
    by_topic = {}
    for turn in batch:
        for tag in tag_turn(turn):
            by_topic.setdefault(tag, []).append(turn)
    topic_summaries = storage.load_topics(sender_id)
    compacted_any = False
    for topic, topic_turns in by_topic.items():
        try:
            new_summary = summarize_fn(topic_summaries.get(topic, ""),
                                       "\n".join(topic_turns))
        except Exception as e:
            logger.warning(f"Compaction skipped for {sender_id}/{topic}: {e}")
            continue
        if not new_summary or estimate_tokens(new_summary) > TOPIC_SUMMARY_MAX_TOKENS * 3:
            logger.warning(f"Compaction produced an unusable summary for {sender_id}/{topic}; keeping the old one")
            continue
        storage.save_topic(sender_id, topic, new_summary.strip(), len(topic_turns))
        compacted_any = True
    if compacted_any:
        storage.save_state(sender_id,
                           compacted_count=state["compacted_count"] + len(batch))
    return compacted_any
