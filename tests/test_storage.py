"""Tests for the history repair that keeps Gemini's function-call turn ordering intact.

These exercise repair_history directly rather than through the database: the rule
being enforced is about the shape of a conversation, and it has to hold whether or
not Postgres is reachable.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage


def _text(role, body):
    return {"role": role, "parts": [{"text": body}]}


def _call(name):
    return {"role": "model", "parts": [{"function_call": {"name": name, "args": {}}}]}


def _response(name):
    return {"role": "user", "parts": [{"function_response": {"name": name, "response": {}}}]}


def test_an_orphaned_response_at_the_head_is_dropped():
    # Exactly the shape that took production down: the trim cut between
    # save_to_long_term_memory's call and its response.
    broken = [_response("save_to_long_term_memory"), _text("model", "שמרתי")]
    assert storage.repair_history(broken) == [_text("model", "שמרתי")]


def test_a_response_that_follows_its_call_is_kept():
    intact = [_text("user", "מה יש במייל"), _call("read_email"), _response("read_email"),
              _text("model", "הנה")]
    assert storage.repair_history(intact) == intact


def test_several_responses_to_one_call_turn_all_survive():
    # The model can emit parallel calls answered over more than one turn; only the
    # first response is adjacent to the call, and dropping the rest would lose data.
    history = [_call("read_email"), _response("read_email"),
               _response("read_email_attachment"), _text("model", "סיימתי")]
    assert storage.repair_history(history) == history


def test_a_trailing_call_with_no_response_is_dropped():
    # The mirror image: sending the next user turn after a dangling call is
    # rejected by the same validation rule.
    history = [_text("user", "היי"), _call("read_email")]
    assert storage.repair_history(history) == [_text("user", "היי")]


def test_a_response_after_a_plain_text_turn_is_an_orphan():
    history = [_text("user", "היי"), _response("read_email"), _text("model", "כן")]
    assert storage.repair_history(history) == [_text("user", "היי"), _text("model", "כן")]


def test_trimming_never_leaves_a_history_starting_with_a_response():
    # Build a conversation whose call/response pair straddles every possible cut
    # point, and check the boundary after the real trim, not just in isolation.
    long_history = []
    for i in range(60):
        long_history.append(_text("user", f"הודעה {i}"))
        long_history.append(_call("save_to_long_term_memory"))
        long_history.append(_response("save_to_long_term_memory"))
    for cut in range(1, len(long_history)):
        repaired = storage.repair_history(long_history[-cut:])
        assert not (repaired and storage._is_response(repaired[0])), f"cut={cut}"


def test_repair_is_idempotent():
    broken = [_response("save_to_long_term_memory"), _text("model", "שמרתי"), _call("x")]
    once = storage.repair_history(broken)
    assert storage.repair_history(once) == once


def test_an_empty_or_malformed_history_does_not_raise():
    assert storage.repair_history([]) == []
    assert storage.repair_history(None) == []
    assert storage.repair_history(["not a dict"]) == ["not a dict"]


def test_camel_case_keys_are_recognised_too():
    # The SDK dumps snake_case, but a row written by an older or different
    # serialisation must not slip an orphan past the check.
    broken = [{"role": "user", "parts": [{"functionResponse": {"name": "x", "response": {}}}]},
              _text("model", "ok")]
    assert storage.repair_history(broken) == [_text("model", "ok")]
