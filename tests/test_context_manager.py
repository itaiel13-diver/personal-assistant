"""The context layer: bounded at any conversation length, follow-ups still
resolve, pins never get summarised, raw history never gets deleted."""

import context_manager as cm
import storage


def _history(n, fact_turn=None, fact_at=5):
    turns = []
    for i in range(n):
        text = f"תור מספר {i} עם קצת תוכן מיותר שמנפח את הטוקנים " * 3
        if i == fact_at and fact_turn:
            text = fact_turn
        turns.append({"role": "user" if i % 2 == 0 else "model",
                      "parts": [{"text": text}]})
    return turns


def _patch(monkeypatch, history, memory=None, state=None, topics=None):
    monkeypatch.setattr(storage, "load_history", lambda s: history)
    monkeypatch.setattr(storage, "load_memory", lambda: memory or {})
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "load_state",
                        lambda s: state or {"summary": "", "pin": {}, "compacted_count": 0})
    monkeypatch.setattr(storage, "load_topics", lambda s: topics or {})


def test_bundle_stays_bounded_across_a_long_conversation(monkeypatch):
    _patch(monkeypatch, _history(200),
           memory={f"fact{i}": {"value": "ערך ארוך " * 20} for i in range(100)})
    for incoming in ["מה קורה?", "משימה בטודו", "פגישה ביומן", ""]:
        bundle = cm.build_context("s", incoming)
        assert bundle["estimated_tokens"] <= cm.CONTEXT_TOKEN_BUDGET, incoming
        assert len(bundle["recent"]) <= cm.RECENT_TURNS


def test_a_contextual_followup_resolves_from_old_turns(monkeypatch):
    _patch(monkeypatch, _history(60, fact_turn="מספר החוג של דנה הוא 4242"))
    bundle = cm.build_context("s", "מה מספר החוג של דנה?")
    assert any("4242" in line for line in bundle["retrieved"])


def test_a_turn_can_belong_to_several_topics():
    assert cm.tag_turn("תוסיף משימה בטודו על הפגישה ביומן") == {"todo", "calendar"}
    assert cm.tag_turn("בוקר טוב") == {"general"}


def test_matched_topic_summaries_ride_the_bundle(monkeypatch):
    _patch(monkeypatch, _history(40),
           topics={"todo": "סוכם: 10 משימות חנויות, 8 חסרות", "money": "0.35 ביטקוין"})
    bundle = cm.build_context("s", "מה המצב עם משימות החנויות?")
    assert any("8 חסרות" in line for line in bundle["retrieved"])
    bundle2 = cm.build_context("s", "כמה ביטקוין יש לי?")
    assert any("0.35" in line for line in bundle2["retrieved"])


def test_compaction_writes_per_topic_digests_and_keeps_pins(monkeypatch):
    saved_topics, saved_state = [], []
    history = _history(cm.RECENT_TURNS + cm.COMPACT_THRESHOLD + 1)
    history[0] = {"role": "user", "parts": [{"text": "משימת טודו: לקנות חלב"}]}
    _patch(monkeypatch, history,
           state={"summary": "", "pin": {"pending_plan": "למחוק DUP1"}, "compacted_count": 0})
    monkeypatch.setattr(storage, "save_topic",
                        lambda s, t, summary, n: saved_topics.append((t, summary, n)))
    monkeypatch.setattr(storage, "save_state",
                        lambda s, **kw: saved_state.append(kw))
    ok = cm.maybe_compact("s", lambda existing, turns: "תקציר: " + turns[:40])
    assert ok
    assert any(t == "todo" for t, _, _ in saved_topics)
    # pins live in their own store and were never part of the digest input
    assert all("למחוק DUP1" not in summary for _, summary, _ in saved_topics)
    bundle = cm.build_context("s", "משימה")
    assert bundle["pin"] == ["pending_plan: למחוק DUP1"]


def test_a_failed_summariser_changes_nothing(monkeypatch):
    calls = []
    _patch(monkeypatch, _history(cm.RECENT_TURNS + cm.COMPACT_THRESHOLD + 1))
    monkeypatch.setattr(storage, "save_topic", lambda *a, **kw: calls.append(a))

    def boom(existing, turns):
        raise RuntimeError("quota")

    assert cm.maybe_compact("s", boom) is False
    assert calls == []


def test_migration_long_history_no_state_no_deletion(monkeypatch):
    history = _history(150)
    _patch(monkeypatch, history)  # no state row, no topics: the pre-layer world
    bundle = cm.build_context("s", "תור מספר 100")
    assert bundle["estimated_tokens"] <= cm.CONTEXT_TOKEN_BUDGET
    assert bundle["total_turns"] == 150  # raw history intact, only the bundle is bounded
