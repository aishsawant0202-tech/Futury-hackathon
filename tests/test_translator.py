"""Translator agent with a fake model: no gateway, no key, same result every time."""
import pytest
from pydantic import ValidationError

from specpilot import db, llm
from specpilot.agents import translator
from specpilot.agents.translator import CardDraft, TranslatorError, translate
from specpilot.tools import gold
from tests.test_rule_picker import GOLD_RULES, NOISE

CARD = "REQ-41-PRECONDITION"


def good_answer(**changes) -> dict:
    """What a correct model answer for the fixture rules (R-3.12-001..007) looks like."""
    answer = {
        "logic": "AND",
        "criteria": [
            {"fact": "current_certified", "op": "=", "value": True, "source_rule_ids": ["R-3.12-001"],
             "quote": "eine Arbeitsunfähigkeitsbescheinigung vorliegt"},
            {"fact": "prior_certified_6m", "op": "=", "value": True, "source_rule_ids": ["R-3.12-002"],
             "quote": "Innerhalb der letzten 6 Monate ... bescheinigte Vorerkrankung"},
            {"fact": "total_days_12m", "op": ">=", "value": 30, "source_rule_ids": ["R-3.12-003"],
             "quote": "zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen"},
            {"fact": "statutory_insured", "op": "=", "value": True, "source_rule_ids": ["R-3.12-004"],
             "quote": "Für privat krankenversicherte ... ist die Anfrage nicht möglich"},
            {"fact": "marginal_employment", "op": "=", "value": False, "source_rule_ids": ["R-3.12-004"],
             "quote": "geringfügig beschäftigte Arbeitnehmer ist die Anfrage nicht möglich"},
        ],
        "unused_rules": [{"rule_id": f"R-3.12-00{n}", "reason": "about what is reported"} for n in (5, 6, 7)],
        "missing_facts": [],
    }
    return answer | changes


def with_criterion(n, **changes) -> list[dict]:
    cs = good_answer()["criteria"]
    cs[n - 1] = cs[n - 1] | changes
    return cs


class FakeModel:
    """Returns scripted answers in order (the last one repeats) and records every prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.users: list[str] = []
        self.systems: list[str] = []

    def __call__(self, model, system, user, schema):
        assert schema is CardDraft
        self.systems.append(system)
        self.users.append(user)
        llm.calls.append({"model": model, "cache_hit": False, "duration_s": 0.1})
        answer = self.answers[min(len(self.users), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return CardDraft.model_validate(answer)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "rules.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def run(conn, monkeypatch, *answers, **kw):
    fake = FakeModel(*answers)
    monkeypatch.setattr(llm, "ask_json", fake)
    return translate(conn, CARD, "fake-model", **kw), fake


# --- the happy path ---------------------------------------------------------------------------

def test_good_answer_becomes_a_correct_draft_card(conn, monkeypatch):
    card, fake = run(conn, monkeypatch, good_answer())
    assert [c.id for c in card.criteria] == ["C1", "C2", "C3", "C4", "C5"]  # numbered by code
    assert card.status.value == "draft" and card.produced_by == "fake-model"
    assert card.source_rule_ids == [f"R-3.12-00{n}" for n in range(1, 8)]
    assert gold.mark(card, gold.load(CARD)).all_correct
    assert len(fake.users) == 2  # first run + cross-check


def test_reviewer_warnings_from_the_pick_are_on_the_card(conn, monkeypatch):
    card, _ = run(conn, monkeypatch, good_answer())
    assert any("R-3.12-005: found in only 1" in f for f in card.flags)
    assert any("R-3.12-006: Contradiction" in f for f in card.flags)


def test_prompt_holds_the_picked_rules_facts_and_operator_words_only(conn, monkeypatch):
    _, fake = run(conn, monkeypatch, good_answer())
    user, system = fake.users[0], fake.systems[0]
    for r in GOLD_RULES:
        assert r.id in user and r.quote in user and r.statement in user
    for r in NOISE:
        assert r.id not in user  # left-out rules never reach the model
    assert "Contradiction with R-3.12-007" in user
    for name in ("current_certified", "total_days_12m", "marginal_employment"):
        assert f"- {name}:" in system
    assert "„mindestens“ -> >=" in system and "„nicht mehr als“ -> <=" in system


def test_worked_example_is_not_the_answer_key(conn, monkeypatch):
    _, fake = run(conn, monkeypatch, good_answer())
    system = fake.systems[0]
    assert "R-9.9-001" in system  # the invented example
    assert "R-3.12-90" not in system and "PLACEHOLDER" not in system and "REQ-41" not in system


def test_example_answer_in_the_prompt_is_itself_valid():
    a = translator.EXAMPLE_ANSWER
    assert {c.fact for c in a.criteria} == {"current_certified", "total_days_12m"}
    assert a.missing_facts[0].rule_ids == ["R-9.9-002"]


def test_audit_rows(conn, monkeypatch):
    run(conn, monkeypatch, good_answer())
    actions = [e.action for e in db.list_audit(conn, CARD)]
    assert any(a.startswith("translate attempt 1: card with 5 criteria") for a in actions)
    assert any(a.startswith("cross-check attempt 1") for a in actions)
    final = [e for e in db.list_audit(conn, CARD) if e.action.startswith("draft card")]
    assert final and "rules_hash=" in final[0].detail and "content_hash=" in final[0].detail


# --- refused cards go back to the model ---------------------------------------------------------

def test_card_citing_a_left_out_rule_is_sent_back_with_the_reason(conn, monkeypatch):
    bad = good_answer(criteria=with_criterion(1, source_rule_ids=["R-3.12-011"]))
    card, fake = run(conn, monkeypatch, bad, good_answer())
    assert "R-3.12-011 is cited but is not in source_rule_ids" in fake.users[1]
    assert "Your previous card was refused" in fake.users[1]
    assert gold.mark(card, gold.load(CARD)).all_correct
    actions = [e.action for e in db.list_audit(conn, CARD)]
    assert "translate attempt 1: card refused" in actions


def test_card_that_ignores_a_rule_is_sent_back(conn, monkeypatch):
    bad = good_answer(unused_rules=[{"rule_id": "R-3.12-005", "reason": "about counting"}])
    _, fake = run(conn, monkeypatch, bad, good_answer())
    assert "not accounted for" in fake.users[1] and "R-3.12-006" in fake.users[1]


def test_gives_up_after_the_attempts(conn, monkeypatch):
    bad = good_answer(unused_rules=[])
    with pytest.raises(TranslatorError, match="no valid card after 3 attempts"):
        run(conn, monkeypatch, bad)


def test_no_valid_json_at_all(conn, monkeypatch):
    with pytest.raises(TranslatorError, match="no valid card JSON"):
        run(conn, monkeypatch, ValueError("fake-model did not return valid CardDraft after 3 attempts"))


@pytest.mark.parametrize("criterion", [
    {"fact": "sum_days", "op": ">=", "value": 30},           # invented fact
    {"fact": "total_days_12m", "op": "~", "value": 30},      # invented operator
    {"fact": "total_days_12m", "op": ">=", "value": "30"},   # number as text
    {"fact": "current_certified", "op": ">", "value": True},  # operator does not fit the fact
])
def test_schema_refuses_invented_facts_operators_and_types(criterion):
    with pytest.raises(ValidationError):
        CardDraft.model_validate(good_answer(criteria=with_criterion(3, **criterion)))


# --- cross-check ------------------------------------------------------------------------------------

def test_cross_check_sends_the_rules_in_reverse_order(conn, monkeypatch):
    _, fake = run(conn, monkeypatch, good_answer())
    first, second = fake.users
    assert first.index("R-3.12-001") < first.index("R-3.12-007")
    assert second.index("R-3.12-007") < second.index("R-3.12-001")


def test_agreeing_runs_add_no_flag(conn, monkeypatch):
    card, _ = run(conn, monkeypatch, good_answer())
    assert not any(f.startswith("Cross-check") for f in card.flags)


def test_disagreeing_runs_are_flagged(conn, monkeypatch):
    second = good_answer(criteria=with_criterion(3, op=">"))
    card, _ = run(conn, monkeypatch, good_answer(), second)
    flags = [f for f in card.flags if f.startswith("Cross-check")]
    assert flags == ["Cross-check (rules in reverse order) disagrees on C3 total_days_12m >= 30: wrong C3 total_days_12m > 30"]
    assert card.criteria[2].op == ">="  # the first run's card is kept


def test_cross_check_also_compares_unused_rules(conn, monkeypatch):
    second = good_answer(unused_rules=[{"rule_id": "R-3.12-005", "reason": "x" * 5}, {"rule_id": "R-3.12-006", "reason": "x" * 5}],
                         missing_facts=[{"rule_ids": ["R-3.12-007"], "quote": "12 Monate", "needed": "months reported back"}])
    card, _ = run(conn, monkeypatch, good_answer(), second)
    assert any("unused_rules differ" in f for f in card.flags) and any("missing_facts differ" in f for f in card.flags)


def test_failed_cross_check_is_flagged_not_fatal(conn, monkeypatch):
    card, _ = run(conn, monkeypatch, good_answer(), ValueError("timeout"))
    assert any(f.startswith("Cross-check run failed") for f in card.flags)


def test_cross_check_can_be_switched_off(conn, monkeypatch):
    _, fake = run(conn, monkeypatch, good_answer(), cross_check=False)
    assert len(fake.users) == 1


def test_picker_errors_pass_through(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeModel(good_answer()))
    with pytest.raises(translator.rule_picker.PickError, match="run the extractor first"):
        translate(db.connect(tmp_path / "empty.db"), CARD, "fake-model")
