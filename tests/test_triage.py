"""Tests for the three drawers.

The asymmetry is the thing being protected. A newsletter that slips through is
a buzz Itai glances at; a question from a store manager that lands in the ignore
drawer is a customer waiting three days for an answer he never knew was wanted.
So the tests are lopsided on purpose: a handful check that bulk mail is silenced,
and the rest check that nothing else ever is.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import triage
from triage import IGNORE, NOTIFY, REPLY


def mail(id="1", sender="dana@impact.co.il", subject="נושא", snippet="גוף ההודעה",
         list_unsubscribe="", labels=None):
    return {"id": id, "sender": sender, "subject": subject, "snippet": snippet,
            "list_unsubscribe": list_unsubscribe, "labels": labels or ["INBOX", "UNREAD"]}


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """Rules only, unless a test says otherwise.

    Without this a machine with a GROQ_API_KEY in its environment would send
    every test's fixture mail over the network to be sorted, which is both slow
    and non-deterministic.
    """
    monkeypatch.setattr(triage.llm, "ask_json", lambda *a, **k: None)


@pytest.fixture
def model(monkeypatch):
    """Installs a fake model and records what it was asked."""
    asked = []

    def install(answer):
        def fake(prompt, system="", max_tokens=600, skip=()):
            asked.append({"prompt": prompt, "system": system, "skip": skip})
            return answer
        monkeypatch.setattr(triage.llm, "ask_json", fake)
        return asked
    return install


# --- what gets silenced --------------------------------------------------


def test_a_list_unsubscribe_header_is_enough_on_its_own():
    # Present on everything sent through a mailing platform and on nothing a
    # colleague types by hand. The single strongest free signal there is.
    assert triage.by_rules(mail(list_unsubscribe="<https://x.com/u/1>")) == IGNORE


def test_gmails_own_promotions_label_is_believed():
    assert triage.by_rules(mail(labels=["INBOX", "CATEGORY_PROMOTIONS"])) == IGNORE


def test_a_no_reply_address_is_a_machine():
    assert triage.by_rules(mail(sender="no-reply@samsung.com")) == IGNORE
    assert triage.by_rules(mail(sender="noreply@connecteam.com")) == IGNORE
    assert triage.by_rules(mail(sender="DoNotReply@impact.co.il")) == IGNORE


def test_a_bounce_is_not_worth_a_buzz():
    assert triage.by_rules(mail(sender="mailer-daemon@googlemail.com")) == IGNORE


def test_an_unsubscribe_footer_in_the_preview_is_enough():
    assert triage.by_rules(mail(snippet="... unsubscribe from this list")) == IGNORE
    assert triage.by_rules(mail(snippet="להסרה מרשימת התפוצה לחצו כאן")) == IGNORE


def test_bulk_beats_a_call_to_action():
    # "Please confirm your email address" from a mailing platform is still
    # bulk. Structure outranks wording in both directions.
    assert triage.by_rules(
        mail(sender="no-reply@shop.com", subject="Please confirm your subscription")
    ) == IGNORE


# --- what is never silenced ----------------------------------------------


def test_wording_alone_never_silences_a_person():
    # Every word here is one his actual job is made of. A lexical rule that
    # dropped these would be dropping his work.
    for subject in ("מבצע חדש בסניף רמלה", "עדכון מחירון", "הנחה על S25 בקיוסק",
                    "דיווח מכירות שבועי"):
        assert triage.by_rules(mail(subject=subject)) != IGNORE


def test_a_person_writing_about_a_newsletter_is_not_a_newsletter():
    # The word appears, but not as the footer of one, and the sender is human.
    # Topic words are not evidence; only boilerplate and headers are.
    assert triage.triage([mail(subject="הניוזלטר של סמסונג", id="7")]) == {"7": NOTIFY}
    assert triage.by_rules(mail(subject="Webinar summary from Dana")) is None


def test_an_unsettled_email_is_notified_when_there_is_no_model():
    # The no-model path is the shipped default until a Groq key is set, and it
    # has to behave exactly as the assistant did before drawers existed.
    assert triage.triage([mail(id="9")]) == {"9": NOTIFY}


def test_an_email_the_model_never_answered_for_is_notified(model):
    model({"2": "ignore"})
    assert triage.triage([mail(id="a"), mail(id="b")]) == {"a": NOTIFY, "b": IGNORE}


# --- what gets flagged ---------------------------------------------------


def test_an_explicit_ask_is_a_reply():
    assert triage.by_rules(mail(snippet="מחכה לתשובה שלך עד מחר")) == REPLY
    assert triage.by_rules(mail(subject="נא לאשר את ההזמנה")) == REPLY
    assert triage.by_rules(mail(snippet="Please confirm by tomorrow")) == REPLY
    assert triage.by_rules(mail(subject="Action required")) == REPLY


def test_a_question_mark_alone_is_not_an_ask():
    # Half the subject lines in any inbox end in one. Left to the model.
    assert triage.by_rules(mail(subject="ראית את זה?")) is None


# --- the model step ------------------------------------------------------


def test_the_model_only_sees_what_the_rules_could_not_settle(model):
    asked = model({"1": "notify"})
    triage.triage([
        mail(id="bulk", sender="no-reply@x.com"),
        mail(id="ask", snippet="מחכה לתשובה"),
        mail(id="unclear", subject="שאלה קצרה"),
    ])
    assert len(asked) == 1
    prompt = asked[0]["prompt"]
    assert "שאלה קצרה" in prompt
    assert "מחכה לתשובה" not in prompt
    assert "no-reply@x.com" not in prompt


def test_one_request_sorts_the_whole_batch(model):
    asked = model({"1": "notify", "2": "ignore", "3": "reply"})
    verdicts = triage.triage([mail(id="x"), mail(id="y"), mail(id="z")])
    assert len(asked) == 1
    assert verdicts == {"x": NOTIFY, "y": IGNORE, "z": REPLY}


def test_the_model_step_never_spends_itais_gemini_quota(model):
    # Twenty requests a day, and they belong to the questions he asked. The
    # waterfall's other tiers are what pay for reading his mail.
    asked = model({"1": "notify"})
    triage.triage([mail(id="x")])
    assert asked[0]["skip"] == ("gemini",)


def test_a_drawer_the_model_invented_is_discarded(model):
    model({"1": "urgent"})
    assert triage.triage([mail(id="x")]) == {"x": NOTIFY}


def test_an_index_the_model_invented_is_discarded(model):
    model({"1": "ignore", "9": "ignore", "zero": "ignore"})
    assert triage.triage([mail(id="x")]) == {"x": IGNORE}


def test_a_model_answering_with_prose_changes_nothing(model):
    model("I think the first one is important")
    assert triage.triage([mail(id="x")]) == {"x": NOTIFY}


def test_a_model_that_raises_does_not_lose_the_mail(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("groq is down")
    monkeypatch.setattr(triage.llm, "ask_json", explode)
    assert triage.triage([mail(id="x")]) == {"x": NOTIFY}


def test_only_a_batch_at_a_time_is_described_to_the_model(model):
    asked = model({})
    many = [mail(id=str(i), subject=f"נושא {i}") for i in range(20)]
    verdicts = triage.triage(many)
    assert f"נושא {triage.MODEL_BATCH - 1}" in asked[0]["prompt"]
    assert f"נושא {triage.MODEL_BATCH}" not in asked[0]["prompt"]
    # The ones past the batch are not lost - they are simply not sorted.
    assert len(verdicts) == 20
    assert set(verdicts.values()) == {NOTIFY}


def test_the_model_is_not_called_when_the_rules_settled_everything(model):
    asked = model({"1": "ignore"})
    triage.triage([mail(id="x", sender="no-reply@x.com")])
    assert asked == []


def test_every_message_comes_back_with_a_drawer():
    verdicts = triage.triage([mail(id="a"), mail(id="b", sender="no-reply@x.com")])
    assert set(verdicts) == {"a", "b"}
    assert all(v in triage.DRAWERS for v in verdicts.values())


def test_a_message_missing_every_field_does_not_crash():
    assert triage.triage([{"id": "bare"}]) == {"bare": NOTIFY}
