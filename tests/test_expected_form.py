"""Expected-result form: consistent results build, inconsistent or incomplete ones are refused."""
import pytest
from pydantic import ValidationError

from specpilot.expected import ConditionResult, ExpectedFile, ExpectedResult, reading_key

HASH = "a" * 64

# the five reason 41 conditions, with the values of one hand-made person (23 days: C3 fails)
CONDS = [("C1", "current_certified", "=", True, True), ("C2", "prior_certified_6m", "=", True, True),
         ("C3", "total_days_12m", ">=", 30, 23), ("C4", "statutory_insured", "=", True, True),
         ("C5", "marginal_employment", "=", False, False)]


def cond(cid, fact, op, limit, value, **changes) -> dict:
    from specpilot.tools import facts
    return {"criterion_id": cid, "fact": fact, "op": op, "limit": limit, "value": value,
            "met": facts.holds(value, op, limit), "rule_ids": [f"R-3.12-00{cid[1]}"], "quote": "Testzitat"} | changes


def result(person="REQ-41-X-B05-P1", neutral="DUMMY-0001", values=None, reading=None, logic="AND", **changes) -> dict:
    conds = [cond(c, f, o, lim, v) for (c, f, o, lim, _), v in zip(CONDS, values or [x[4] for x in CONDS])]
    met = [c["met"] for c in conds]
    return {"person_id": person, "neutral_id": neutral, "reading": reading or {}, "logic": logic,
            "conditions": conds, "message_expected": all(met) if logic == "AND" else any(met)} | changes


def file(results=None, people=("REQ-41-X-B05-P1",), readings=({},), **changes) -> dict:
    return {"card_id": "REQ-41-X", "card_version": 1, "card_hash": HASH, "seed": 4711,
            "dummy_file": "REQ-41-X-v1-seed4711.sqlite", "dummy_sha256": "b" * 64, "logic": "AND", "n_criteria": 5,
            "person_ids": list(people), "readings": list(readings),
            "results": results if results is not None else [result()]} | changes


# --- one condition ------------------------------------------------------------------------------------

def test_condition_result_and_text():
    c = ConditionResult.model_validate(cond(*CONDS[2]))
    assert not c.met and c.condition() == "total_days_12m >= 30"
    assert c.text() == "C3 ✘ total_days_12m >= 30 (person: 23) - R-3.12-003: „Testzitat“"


@pytest.mark.parametrize("changes, message", [
    ({"met": True}, "does not follow"),             # 23 >= 30 is not met
    ({"value": True}, "is a number"),               # a yes/no value for a number fact
    ({"value": "23"}, "value"),                     # text instead of a number
    ({"op": "~"}, "operator"),
    ({"fact": "shoe_size"}, "unknown fact"),
    ({"rule_ids": []}, "at least 1"),
    ({"extra": 1}, "Extra inputs"),
])
def test_inconsistent_condition_results_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        ConditionResult.model_validate(cond(*CONDS[2]) | changes)


def test_yes_no_value_for_a_yes_no_fact():
    with pytest.raises(ValidationError, match="is yes/no"):
        ConditionResult.model_validate(cond(*CONDS[0]) | {"value": 1})


# --- one person -------------------------------------------------------------------------------------------

def test_message_follows_from_the_conditions():
    r = ExpectedResult.model_validate(result())
    assert r.message_expected is False
    assert ExpectedResult.model_validate(result(values=[True, True, 45, True, False])).message_expected is True
    assert ExpectedResult.model_validate(result(logic="OR")).message_expected is True


def test_wrong_message_is_refused():
    with pytest.raises(ValidationError, match="does not follow from AND"):
        ExpectedResult.model_validate(result(message_expected=True))


def test_conditions_in_order():
    r = result()
    r["conditions"] = r["conditions"][::-1]
    with pytest.raises(ValidationError, match="C1, C2, ... in order"):
        ExpectedResult.model_validate(r)


def test_explanation_names_every_condition_and_its_rule():
    text = ExpectedResult.model_validate(result()).explanation()
    assert text.startswith("DUMMY-0001 (REQ-41-X-B05-P1), reading main: message must NOT be sent")
    assert "C3 ✘ total_days_12m >= 30 (person: 23) - R-3.12-003" in text
    assert text.count("✔") == 4


# --- the whole file ------------------------------------------------------------------------------------------

def test_valid_file_and_summary():
    f = ExpectedFile.model_validate(file())
    assert f.summary() == ("REQ-41-X v1, seed 4711: 1 people x 1 reading(s) = 1 expected results; "
                           "message must be sent in 0, must not be sent in 1")
    assert ExpectedFile.model_validate_json(f.model_dump_json()) == f


def test_one_result_per_person_and_reading():
    two = ("REQ-41-X-B05-P1", "REQ-41-X-B01-P1")
    with pytest.raises(ValidationError, match="missing"):
        ExpectedFile.model_validate(file(people=two))
    ok = file(people=two, results=[result(), result("REQ-41-X-B01-P1", "DUMMY-0002", values=[True, True, 45, True, False])])
    assert len(ExpectedFile.model_validate(ok).results) == 2
    with pytest.raises(ValidationError, match="more than one result"):
        ExpectedFile.model_validate(file(results=[result(), result()]))


def test_every_reading_needs_its_own_results():
    strict = {"C3": "strict"}
    with pytest.raises(ValidationError, match="missing"):
        ExpectedFile.model_validate(file(readings=({}, strict)))
    ok = file(readings=({}, strict), results=[result(), result(reading=strict)])
    assert ExpectedFile.model_validate(ok).summary().endswith("= 2 expected results; message must be sent in 0, "
                                                               "must not be sent in 2")
    assert reading_key(strict) == "C3=strict" and reading_key({}) == "main"


@pytest.mark.parametrize("changes, message", [
    ({"results": [result(logic="OR")]}, "logic or number of conditions"),
    ({"n_criteria": 4}, "logic or number of conditions"),
    ({"person_ids": ["REQ-41-X-B05-P1", "REQ-41-X-B05-P1"]}, "appears twice"),
    ({"dummy_sha256": "not-a-fingerprint"}, "dummy_sha256"),
    ({"readings": [{}, {}]}, "listed twice"),
])
def test_inconsistent_files_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        ExpectedFile.model_validate(file(**changes))


def test_one_person_one_neutral_id():
    strict = {"C3": "strict"}
    bad = file(readings=({}, strict), results=[result(), result(reading=strict, neutral="DUMMY-0009")])
    with pytest.raises(ValidationError, match="two different neutral ids"):
        ExpectedFile.model_validate(bad)
