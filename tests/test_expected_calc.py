"""Expected-results calculator: answers from the raw values only, refuses anything it cannot trust."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from specpilot.criteria import CriteriaCard
from specpilot.dummies import build
from specpilot.review import approve
from specpilot.tools import facts
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import load, write_outputs
from specpilot.tools.expected_results import ExpectedError, compute, sha256
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
    """A private copy of the dummy file that a test may damage."""
    return Path(shutil.copy(dummy_path, tmp_path / dummy_path.name))


def edit(path, sql, *args):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(sql, args)
    conn.close()


# --- correct answers --------------------------------------------------------------------------------------

def test_one_result_per_person(card, dummy_path):
    e = compute(card, dummy_path)
    d = load(dummy_path)
    assert [r.person_id for r in e.results] == [p.person_id for p in d.people]
    assert [r.neutral_id for r in e.results] == [p.neutral_id for p in d.people]
    assert sum(r.message_expected for r in e.results) == 2


def test_every_condition_is_recomputed_from_the_raw_values(card, dummy_path):
    d = load(dummy_path)
    for r, p in zip(compute(card, dummy_path).results, d.people):
        values = facts.evaluate(d.scenario_of(p))
        for c, card_c in zip(r.conditions, card.criteria):
            assert (c.fact, c.op, c.limit) == (card_c.fact, card_c.op, card_c.value)
            assert c.value == values[c.fact] and c.met == facts.holds(values[c.fact], c.op, c.limit)
            assert c.rule_ids == card_c.source_rule_ids and c.quote == card_c.quote


def test_edge_people(card, dummy_path):
    e = {r.person_id: r for r in compute(card, dummy_path).results}
    at_30, at_29 = e["REQ-41-PRECONDITION-B01-E1-P1"], e["REQ-41-PRECONDITION-B05-E1-P1"]
    assert at_30.message_expected and at_30.conditions[2].value == 30
    assert not at_29.message_expected and at_29.conditions[2].value == 29
    assert "C3 ✘ total_days_12m >= 30 (person: 29)" in at_29.explanation()


def test_records_where_it_came_from(card, dummy_path):
    e = compute(card, dummy_path)
    assert (e.card_id, e.card_version, e.card_hash, e.seed) == (card.id, card.version, card.content_hash(), 4711)
    assert e.dummy_file == dummy_path.name and e.dummy_sha256 == sha256(dummy_path)


def test_same_file_same_answers(card, dummy_path):
    assert compute(card, dummy_path) == compute(card, dummy_path)


# --- the raw values decide, never the labels -------------------------------------------------------------------

def test_labels_are_not_used(card, copy):
    """Flip design labels in the file (consistently, so the file still reads): the answers must not change,
    because they come from the raw values."""
    before = compute(card, copy)
    edit(copy, "UPDATE dummy_people SET C3_truth = 1 - C3_truth")
    edit(copy, "UPDATE dummy_people SET all_true = C1_truth * C2_truth * C3_truth * C4_truth * C5_truth")
    assert [r.message_expected for r in compute(card, copy).results] == [r.message_expected for r in before.results]


def test_self_contradicting_labels_make_the_file_unreadable(card, copy):
    edit(copy, "UPDATE dummy_people SET all_true = 1 - all_true")
    with pytest.raises(ExpectedError, match="cannot be read as a dummy file"):
        compute(card, copy)


def test_raw_values_are_used(card, copy):
    person = "REQ-41-PRECONDITION-B01-P1"
    edit(copy, "UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = ?", person)
    r = next(r for r in compute(card, copy).results if r.person_id == person)
    assert not r.message_expected and not r.conditions[3].met


# --- readings ------------------------------------------------------------------------------------------------------

def test_two_readings_give_two_answers_per_person(tmp_path):
    from tests.test_branch_maker import alt, card as sized
    c = sized(3, alternatives={3: alt("statutory_insured", "!=", True, "inverted")})
    c = CriteriaCard.model_validate(c.model_dump() | {"status": "approved", "reviewer": "A",
                                                      "reviewed_at": "2027-01-01T00:00:00", "approved_hash": c.content_hash()})
    s = make_branches(c)
    d = build(make_people(s, 1), s, "test")
    e = compute(c, write_outputs(d, check(c, s, d), tmp_path).sqlite)
    assert len(e.results) == 2 * len(d.people)
    by = {(r.person_id, tuple(r.reading.items())): r for r in e.results}
    for p in d.people:
        main, inv = by[(p.person_id, ())], by[(p.person_id, (("C3", "inverted"),))]
        assert main.conditions[2].met != inv.conditions[2].met  # the two readings of C3 are opposites


# --- refusals --------------------------------------------------------------------------------------------------------

def test_unapproved_or_changed_card_is_refused(card, dummy_path):
    with pytest.raises(ExpectedError, match="not approved"):
        compute(draft(), dummy_path)
    with pytest.raises(ExpectedError, match="changed after approval"):
        compute(card.model_copy(update={"title": "changed"}), dummy_path)


def test_dummy_file_of_another_card_version_is_refused(card, dummy_path):
    v2 = approve(CriteriaCard.model_validate(card.model_dump() | {
        "version": 2, "status": "draft", "reviewer": "", "reviewed_at": None, "approved_hash": ""}), "A", RULES)
    with pytest.raises(ExpectedError, match="fingerprints differ"):
        compute(v2, dummy_path)


def test_dummy_file_without_a_passed_self_check_is_refused(card, copy):
    edit(copy, "UPDATE dummy_meta SET value = 'SELF-CHECK FAILED: ...' WHERE key = 'self_check'")
    with pytest.raises(ExpectedError, match="self-check did not pass"):
        compute(card, copy)


def test_missing_broken_or_foreign_files_are_refused(card, tmp_path):
    with pytest.raises(ExpectedError, match="not found"):
        compute(card, tmp_path / "nope.sqlite")
    (tmp_path / "junk.sqlite").write_bytes(b"not a database at all, just text" * 10)
    with pytest.raises(ExpectedError, match="not a dummy file"):
        compute(card, tmp_path / "junk.sqlite")
    other = sqlite3.connect(tmp_path / "other.sqlite")
    other.execute("CREATE TABLE something (x)")
    other.close()
    with pytest.raises(ExpectedError, match="not a dummy file"):
        compute(card, tmp_path / "other.sqlite")


def test_damaged_dummy_rows_are_refused(card, copy):
    edit(copy, "DELETE FROM dummy_people WHERE neutral_id = (SELECT MIN(neutral_id) FROM dummy_people)")
    with pytest.raises(ExpectedError, match="cannot be read as a dummy file"):
        compute(card, copy)


def test_does_not_use_the_teammates_reference_model():
    """Station 6 is the user's own calculator: it must not call the team's reference model."""
    source = Path("specpilot/tools/expected_results.py").read_text(encoding="utf-8")
    assert "reference_model" not in source
