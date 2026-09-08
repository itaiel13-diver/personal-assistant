"""Tests for the one-question-a-day routine's judgement.

What is being protected here is not that a question gets asked - that is the
easy half - but that the assistant never asks the same thing twice. Two things
make that true: a question drops out of the queue the moment its answer is in
long-term memory, and a question that was asked and ignored is never revived.
The rest of these tests are about the model-generated question, which is the
only place an invented, unusable memory key could enter the store.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curiosity
from curiosity import Question

# Captured before the autouse fixture below replaces it, so the two tests
# that are actually about reading memory test the real function.
read_memory = curiosity._memory


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """No question is generated unless a test asks for one.

    Autouse for the same reason it is in the triage tests: a machine with a
    Groq key in its environment must not be able to turn a unit test into a
    network call.
    """
    monkeypatch.setattr(curiosity.llm, "ask_json", lambda *a, **k: None)


@pytest.fixture
def model(monkeypatch):
    """Installs a fake model and records what it was asked."""
    calls = {}

    def install(answer):
        def fake(prompt, system="", max_tokens=600, skip=()):
            calls["prompt"] = prompt
            calls["system"] = system
            calls["skip"] = skip
            return answer
        monkeypatch.setattr(curiosity.llm, "ask_json", fake)
        return calls
    return install


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    """An empty long-term memory by default, so nothing reaches the database."""
    store = {}
    monkeypatch.setattr(curiosity, "_memory", lambda: dict(store))

    def fill(**facts):
        store.clear()
        store.update({k: {"value": v, "category": "test"} for k, v in facts.items()})
        return store
    return fill


def taker(*already_claimed):
    """A claim function backed by a set, like the proactive log is."""
    claimed = set(already_claimed)

    def claim(key):
        if key in claimed:
            return False
        claimed.add(key)
        return True
    claim.claimed = claimed
    return claim


# --- the seeds -----------------------------------------------------------


def test_no_two_seeds_write_to_the_same_memory_key():
    keys = [q.key for q in curiosity.SEEDS]
    assert len(keys) == len(set(keys))


def test_every_seed_key_is_shaped_like_one_the_model_would_be_allowed_to_invent():
    for question in curiosity.SEEDS:
        assert curiosity._KEY.match(question.key), question.key


def test_all_seven_cities_of_his_territory_are_asked_about():
    keys = {q.key for q in curiosity.SEEDS}
    for city in ("rishon_lezion", "ramla", "lod", "kiryat_ono",
                 "kiryat_ekron", "yavne", "or_yehuda"):
        assert f"store_{city}" in keys


def test_the_territory_comes_before_the_preferences():
    """Order is a curriculum, not a list. The gaps that make the assistant
    wrong most often are asked about first."""
    keys = [q.key for q in curiosity.SEEDS]
    assert keys.index("store_rishon_lezion") < keys.index("quiet_hours")


# --- what is still worth asking ------------------------------------------


def test_a_question_he_has_already_answered_is_not_asked_again(memory):
    memory(store_ramla="סניף רמלה, אזור התעשייה")
    assert "store_ramla" not in [q.key for q in curiosity.candidates()]


def test_answering_in_an_ordinary_conversation_counts(memory):
    """Nothing marks a fact as having come from a question. Anything in
    long-term memory closes the gap, however it got there."""
    before = len(curiosity.candidates())
    memory(my_manager="רונן, ronen@samsung.com")
    assert len(curiosity.candidates()) == before - 1


def test_an_empty_memory_leaves_every_seed_on_the_table():
    assert len(curiosity.candidates()) == len(curiosity.SEEDS)


# --- asking ---------------------------------------------------------------


def test_the_first_question_is_the_first_unanswered_one():
    question = curiosity.ask_next(taker())
    assert question.key == curiosity.SEEDS[0].key


def test_a_question_already_asked_is_passed_over():
    first, second = curiosity.SEEDS[0], curiosity.SEEDS[1]
    assert curiosity.ask_next(taker(first.key)).key == second.key


def test_a_question_he_ignored_is_never_revived():
    """The claim is the whole mechanism: silence is an answer, and an assistant
    that re-asks what he chose not to answer is one he stops reading."""
    claim = taker()
    asked = curiosity.ask_next(claim)
    assert curiosity.ask_next(claim).key != asked.key


def test_asking_takes_the_claim_for_the_question_it_returns():
    claim = taker()
    question = curiosity.ask_next(claim)
    assert claim.claimed == {question.key}


def test_nothing_is_asked_once_the_seeds_are_spent_and_no_model_answers():
    claim = taker(*[q.key for q in curiosity.SEEDS])
    assert curiosity.ask_next(claim) is None


def test_the_model_is_not_consulted_while_a_seed_is_still_unasked(model):
    calls = model({"key": "invented", "question": "שאלה?"})
    curiosity.ask_next(taker())
    assert calls == {}


# --- the generated question ----------------------------------------------


def test_a_generated_question_takes_over_when_the_seeds_run_out(model, memory):
    memory(**{q.key: "known" for q in curiosity.SEEDS})
    model({"key": "delivery_days", "question": "באילו ימים מגיעות המשלוחים?", "category": "work"})
    question = curiosity.ask_next(taker())
    assert question == Question("delivery_days", "באילו ימים מגיעות המשלוחים?", "work")


def test_the_generated_question_never_spends_a_gemini_call(model):
    calls = model({"key": "delivery_days", "question": "באילו ימים מגיעות המשלוחים?"})
    curiosity.generated({})
    assert calls["skip"] == ("gemini",)


def test_the_model_is_shown_what_is_already_known(model, memory):
    calls = model(None)
    curiosity.generated({"store_lod": {"value": "סניף לוד", "category": "territory"}})
    assert "סניף לוד" in calls["prompt"]


def test_a_generated_key_that_is_already_known_is_discarded(model):
    model({"key": "store_lod", "question": "איך קוראים לחנות בלוד?"})
    assert curiosity.generated({"store_lod": {"value": "x"}}) is None


@pytest.mark.parametrize("key", ["", "Store Lod", "store-lod", "ab", "1store", "שאלה",
                                 "x" * 60, None])
def test_a_key_that_could_not_be_stored_is_discarded(model, key):
    model({"key": key, "question": "שאלה סבירה לגמרי?"})
    assert curiosity.generated({}) is None


@pytest.mark.parametrize("text", ["", "מה?", "ש" * 400, None])
def test_a_question_of_an_implausible_length_is_discarded(model, text):
    model({"key": "delivery_days", "question": text})
    assert curiosity.generated({}) is None


def test_prose_instead_of_json_asks_nothing(model):
    model(None)
    assert curiosity.generated({}) is None


def test_a_list_instead_of_an_object_asks_nothing(model):
    model([{"key": "delivery_days", "question": "שאלה?"}])
    assert curiosity.generated({}) is None


def test_a_generated_question_with_no_category_still_gets_stored_somewhere(model):
    model({"key": "delivery_days", "question": "באילו ימים מגיעות המשלוחים?"})
    assert curiosity.generated({}).category == "general"


def test_a_model_that_raises_loses_the_question_and_nothing_else(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no provider")
    monkeypatch.setattr(curiosity.llm, "ask_json", broken)
    with pytest.raises(RuntimeError):
        curiosity.generated({})


# --- reading memory -------------------------------------------------------


def test_memory_that_cannot_be_read_leaves_every_question_askable(monkeypatch):
    """A database that is down must not make the assistant think it already
    knows everything - the failure mode has to be asking, not silence."""
    def down():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(curiosity.storage, "enabled", lambda: True)
    monkeypatch.setattr(curiosity.storage, "load_memory", down)
    monkeypatch.setattr(curiosity, "MEMORY_FILE", "/nonexistent/memory.json")
    assert read_memory() == {}
    assert len(curiosity.candidates(read_memory())) == len(curiosity.SEEDS)


def test_with_no_database_and_no_file_memory_is_simply_empty(monkeypatch):
    monkeypatch.setattr(curiosity.storage, "enabled", lambda: False)
    monkeypatch.setattr(curiosity, "MEMORY_FILE", "/nonexistent/memory.json")
    assert read_memory() == {}


def test_the_file_is_read_when_there_is_no_database(monkeypatch, tmp_path):
    """The fallback store, for a machine running without Postgres."""
    path = tmp_path / "long_term_memory.json"
    path.write_text('{"store_lod": {"value": "\u05e1\u05e0\u05d9\u05e3 \u05dc\u05d5\u05d3"}}', encoding="utf-8")
    monkeypatch.setattr(curiosity.storage, "enabled", lambda: False)
    monkeypatch.setattr(curiosity, "MEMORY_FILE", str(path))
    assert "store_lod" in read_memory()


def test_a_corrupt_memory_file_is_not_mistaken_for_knowing_everything(monkeypatch, tmp_path):
    path = tmp_path / "long_term_memory.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(curiosity.storage, "enabled", lambda: False)
    monkeypatch.setattr(curiosity, "MEMORY_FILE", str(path))
    assert read_memory() == {}
