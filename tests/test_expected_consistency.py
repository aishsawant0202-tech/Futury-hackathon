"""Consistency check: every answer agrees with the person's design label, or the disagreement is reported."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from specpilot.criteria import CriteriaCard
from specpilot.dummies import build
from specpilot.review import approve
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import load, write_outputs
from specpilot.tools.expected_results import ExpectedError, check_consistency, compute, design_reading
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def dummy_path(card, tmp_path_factory):
    s = make_branches(card, edges=True)
    d = build(make_people(s, 4711), s, "test")
    return write_outputs(d, check(card, s, d), tmp_path_factory.mktemp("dummies")).sqlite


@pytest.fixture
def copy(dummy_path, tmp_path):
    return Path(shutil.copy(dummy_path, tmp_path / dummy_path.name))


def edit(path, sql):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(sql)
    conn.close()


def consistency(card, path):
    return check_consistency(compute(card, path), load(path))


# --- agreement ------------------------------------------------------------------------------------------------

def test_correct_file_is_consistent(card, dummy_path):
    c = consistency(card, dummy_path)
    assert c.ok and c.compared == 34 and not c.notes
    assert c.text() == "CONSISTENCY PASSED: 34 answer(s) compared with the design labels, 0 problem(s)"


# --- disagreements are reported, condition by condition -----------------------------------------------------------

def test_raw_value_changed_after_the_self_check(card, copy):
    edit(copy, "UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = 'REQ-41-PRECONDITION-B01-P1'")
    c = consistency(card, copy)
    assert not c.ok
    assert [(p.check, p.where) for p in c.problems] == [("condition", "REQ-41-PRECONDITION-B01-P1"),
                                                         ("message", "REQ-41-PRECONDITION-B01-P1")]
    assert "C4 statutory_insured = true: designed to be met, but from the raw values statutory_insured = False" \
           in c.problems[0].message


def test_change_that_flips_a_condition_but_not_the_message(card, copy):
    """B02 fails only C5 (Minijob); making it a non-statutory Minijob fails C4 too: still 'must not be sent',
    but the condition labels no longer match - and that is reported."""
    edit(copy, "UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = 'REQ-41-PRECONDITION-B02-P1'")
    c = consistency(card, copy)
    assert [(p.check, p.where) for p in c.problems] == [("condition", "REQ-41-PRECONDITION-B02-P1")]


def test_labels_changed_consistently(card, copy):
    edit(copy, "UPDATE dummy_people SET C3_truth = 1 - C3_truth")
    edit(copy, "UPDATE dummy_people SET all_true = C1_truth * C2_truth * C3_truth * C4_truth * C5_truth")
    c = consistency(card, copy)
    assert not c.ok and sum(p.check == "condition" for p in c.problems) == 34  # every person's C3


def test_results_for_another_dummy_file(card, dummy_path, tmp_path):
    s = make_branches(card, edges=True)
    d = build(make_people(s, 99), s, "test")
    other = write_outputs(d, check(card, s, d), tmp_path).sqlite
    c = check_consistency(compute(card, other), load(dummy_path))
    assert [p.check for p in c.problems] == ["origin"]


def test_people_differ_from_the_dummy_file(card, dummy_path):
    e = compute(card, dummy_path)
    d = load(dummy_path)
    c = check_consistency(e, d.model_copy(update={"people": d.people[:-1]}))
    assert [p.check for p in c.problems] == ["people"] and "unknown" in c.problems[0].message


# --- readings -------------------------------------------------------------------------------------------------------

def test_design_reading_from_the_branch_id():
    readings = [{}, {"C3": "strict"}]
    assert design_reading("REQ-41-X-B07", readings) == {}
    assert design_reading("REQ-41-X-R2-B07", readings) == {"C3": "strict"}
    assert design_reading("REQ-41-X-R2-B07-E1", readings) == {"C3": "strict"}
    with pytest.raises(ExpectedError, match="names reading 3"):
        design_reading("REQ-41-X-R3-B01", readings)


def test_two_readings_compare_each_person_under_its_own_reading(tmp_path):
    from tests.test_branch_maker import alt, card as sized
    c = sized(3, alternatives={3: alt("statutory_insured", "!=", True, "inverted")})
    c = CriteriaCard.model_validate(c.model_dump() | {"status": "approved", "reviewer": "A",
                                                      "reviewed_at": "2027-01-01T00:00:00", "approved_hash": c.content_hash()})
    s = make_branches(c)
    d = build(make_people(s, 1), s, "test")
    path = write_outputs(d, check(c, s, d), tmp_path).sqlite
    result = check_consistency(compute(c, path), load(path))
    assert result.ok and result.compared == len(d.people)
    assert result.notes == [f"{len(d.people)} result(s) for other readings have no design label to compare with "
                            f"(each person is labelled for its own branch's reading only)"]
