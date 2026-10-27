"""Criteria cards: valid cards build, broken cards are refused, approval is tied to the content."""
import random
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.tools.reference_model import P135, expected
from tests.test_facts import P, as_case, random_scenario, scenario

# Test fixture. Rule ids and quotes are stand-ins, not the real extraction output.
QUOTE_30 = "zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen"


def crit(cid, fact, op, value, rules=("R-3.12-001",), quote="Testzitat aus der Spezifikation", **extra):
    return {"id": cid, "fact": fact, "op": op, "value": value, "source_rule_ids": list(rules), "quote": quote, **extra}


def card_data(**overrides) -> dict:
    data = {
        "id": "REQ-41-PRECONDITION",
        "title": "When may reason 41 be sent?",
        "reason_codes": ["41"],
        "kind": "precondition",
        "criteria": [
            crit("C1", "current_certified", "=", True, ("R-3.12-001",)),
            crit("C2", "prior_certified_6m", "=", True, ("R-3.12-002",)),
            crit("C3", "total_days_12m", ">=", 30, ("R-3.12-003",), QUOTE_30),
            crit("C4", "statutory_insured", "=", True, ("R-3.12-004",)),
            crit("C5", "marginal_employment", "=", False, ("R-3.12-004",)),
        ],
        "source_rule_ids": ["R-3.12-001", "R-3.12-002", "R-3.12-003", "R-3.12-004", "R-3.12-005"],
        "unused_rules": [{"rule_id": "R-3.12-005", "reason": "Duty of the insurer, not the employer"}],
    }
    data.update(overrides)
    return data


def card(**overrides) -> CriteriaCard:
    return CriteriaCard.model_validate(card_data(**overrides))


def refused(match: str, **overrides):
    with pytest.raises(ValidationError, match=match):
        card(**overrides)


def with_c(n: int, **changes) -> list[dict]:
    """The fixture's criteria with criterion n (1-based) changed."""
    cs = card_data()["criteria"]
    cs[n - 1] = {**cs[n - 1], **changes}
    return cs


# --- a valid card ------------------------------------------------------------------------

def test_valid_card_builds_and_round_trips():
    c = card()
    assert c.status is CardStatus.DRAFT and c.version == 1
    again = CriteriaCard.model_validate_json(c.model_dump_json())
    assert again == c
    assert again.content_hash() == c.content_hash()


def test_describe_shows_every_criterion_and_its_source():
    text = card().describe()
    for line in ("C1  current_certified = true", "C3  total_days_12m >= 30", "R-3.12-003", "unused R-3.12-005"):
        assert line in text, line


# --- broken sub-criteria are refused -----------------------------------------------------------

def test_unknown_fact():
    refused("unknown fact 'sum_days'", criteria=with_c(3, fact="sum_days"))


def test_operator_must_fit_the_fact():
    refused("operator '>' is not allowed for current_certified", criteria=with_c(1, op=">"))
    refused("operator 'in' is not allowed", criteria=with_c(3, op="in"))


@pytest.mark.parametrize("n, value", [
    (1, "true"), (1, 1), (3, "30"), (3, True), (3, 30.5), (3, None),
])
def test_value_must_have_the_facts_type(n, value):
    with pytest.raises(ValidationError):
        card(criteria=with_c(n, value=value))


def test_between_needs_low_and_high():
    assert card(criteria=with_c(3, op="between", value=[30, 60])).criteria[2].value == [30, 60]
    refused("between needs", criteria=with_c(3, op="between", value=30))
    refused("low 60 is above high 30", criteria=with_c(3, op="between", value=[60, 30]))


def test_criteria_numbered_in_order():
    cs = card_data()["criteria"]
    cs[1]["id"] = "C7"
    refused("numbered C1, C2", criteria=cs)


def test_needs_at_least_one_criterion_and_a_source():
    refused("at least 1", criteria=[])
    refused("at least 1", criteria=with_c(1, source_rule_ids=[]))


def test_rule_ids_have_the_extractor_pattern():
    refused("not a rule id", criteria=with_c(1, source_rule_ids=["rule 1"]))


def test_extra_fields_are_refused():
    refused("Extra inputs", criteria=with_c(1, confidence=0.9))
    refused("Extra inputs", priority="high")


def test_bad_reason_code():
    refused("reason_codes", reason_codes=["40"])


# --- every picked rule is accounted for ------------------------------------------------------

def test_cited_rule_must_be_picked():
    refused("R-3.12-009 is cited but is not in source_rule_ids", criteria=with_c(1, source_rule_ids=["R-3.12-009"]))


def test_every_picked_rule_must_be_accounted_for():
    refused(r"\['R-3.12-005'\] not accounted for", unused_rules=[])
    refused(r"\['R-3.12-006'\] not accounted for", source_rule_ids=card_data()["source_rule_ids"] + ["R-3.12-006"])


def test_rule_cannot_be_used_and_unused():
    refused("cannot be both used and unused", unused_rules=[{"rule_id": "R-3.12-001", "reason": "background text"}])


def test_missing_fact_is_recorded():
    c = card(source_rule_ids=card_data()["source_rule_ids"] + ["R-3.12-006"],
             missing_facts=[{"rule_ids": ["R-3.12-006"], "quote": "nicht bei Minijob-Zentrale",
                             "needed": "whether the employer reports to the Minijob-Zentrale"}])
    assert "missing fact (R-3.12-006)" in c.describe()


def test_duplicate_source_rule():
    refused("lists a rule twice", source_rule_ids=["R-3.12-001", "R-3.12-001", "R-3.12-002", "R-3.12-003",
                                                   "R-3.12-004", "R-3.12-005"])


# --- ambiguity: two readings ---------------------------------------------------------------------

ALT = {"fact": "total_days_12m", "op": ">", "value": 30, "source_rule_ids": ["R-3.12-003"],
       "quote": "mehr als 30 Tage", "label": "other reading"}


def ambiguous_c3(**alt_changes) -> list[dict]:
    return with_c(3, label="p.135 reading", ambiguity="test: two pages word the limit differently",
                  alternatives=[{**ALT, **alt_changes}])


def test_alternative_reading_is_kept():
    c = card(criteria=ambiguous_c3())
    assert c.readings() == {"C3": ["p.135 reading", "other reading"]}
    assert c.conditions({"C3": "other reading"})["C3"].op == ">"
    assert c.conditions()["C3"].op == ">="


def test_alternative_needs_an_explanation_and_labels():
    refused("need an 'ambiguity'", criteria=with_c(3, label="x", alternatives=[ALT]))
    refused("name the main reading", criteria=with_c(3, ambiguity="because", alternatives=[ALT]))


def test_alternative_must_differ_and_be_about_the_same_fact():
    refused("identical to another reading", criteria=ambiguous_c3(op=">="))
    refused("same fact", criteria=ambiguous_c3(fact="current_certified", op="=", value=True))
    refused("both labelled", criteria=ambiguous_c3(label="p.135 reading"))


def test_unknown_reading_or_criterion():
    c = card(criteria=ambiguous_c3())
    with pytest.raises(KeyError):
        c.conditions({"C3": "no such reading"})
    with pytest.raises(KeyError):
        c.conditions({"C9": "other reading"})


# --- approval is tied to the exact content ---------------------------------------------------------

def approved(c: CriteriaCard) -> CriteriaCard:
    return CriteriaCard.model_validate(c.model_dump() | {
        "status": "approved", "reviewer": "examiner", "reviewed_at": datetime(2026, 10, 27, 12, 0),
        "approved_hash": c.content_hash()})


def test_approval_with_matching_content():
    a = approved(card())
    assert a.approval_valid


def test_approval_needs_reviewer_and_time():
    refused("needs the reviewer", status="approved", approved_hash=card().content_hash())


def test_changed_card_loses_its_approval():
    a = approved(card())
    edited = a.model_copy(update={"criteria": with_c_models(a, 3, value=31)})
    assert edited.content_hash() != a.content_hash()
    assert not edited.approval_valid  # in memory
    with pytest.raises(ValidationError, match="changed after it was approved"):
        CriteriaCard.model_validate(edited.model_dump())  # and when loaded again


def with_c_models(c: CriteriaCard, n: int, **changes):
    cs = list(c.criteria)
    cs[n - 1] = cs[n - 1].model_copy(update=changes)
    return cs


def test_review_fields_do_not_change_the_content_hash():
    c = card()
    assert c.model_copy(update={"flags": ["x"], "produced_by": "hand"}).content_hash() == c.content_hash()


def test_rejection_needs_a_reason_and_draft_cannot_carry_approval():
    refused("needs the reviewer, the time and a reason", status="rejected", reviewer="examiner",
            reviewed_at=datetime(2026, 10, 27))
    refused("draft card cannot carry an approval", approved_hash="abc")


# --- using a card on test people -------------------------------------------------------------------

def test_evaluate_one_person():
    c = card()
    assert c.evaluate(scenario()) == {"C1": True, "C2": True, "C3": True, "C4": True, "C5": True}
    s = scenario(previous=[P("2026-06-01", "2026-06-12")], statutory_insured=False)  # 29 days, private
    assert c.evaluate(s) == {"C1": True, "C2": True, "C3": False, "C4": False, "C5": True}
    assert not c.satisfied(s)


def test_choice_of_reading_changes_the_outcome_at_the_edge():
    c = card(criteria=ambiguous_c3())
    exactly_30 = scenario(previous=[P("2026-06-01", "2026-06-13")])
    assert c.satisfied(exactly_30)
    assert not c.satisfied(exactly_30, {"C3": "other reading"})


def test_or_logic():
    s = scenario(statutory_insured=False)
    assert not card().satisfied(s)
    assert card(logic="OR").satisfied(s)


def test_card_agrees_with_reference_model_on_random_people():
    c = card()
    rng = random.Random(2)
    for _ in range(1000):
        s = random_scenario(rng)
        assert c.satisfied(s) == expected(as_case(s), P135).message_expected
