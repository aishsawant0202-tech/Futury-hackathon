"""Dummy file form: built from Station 4's people, every table consistent, broken files refused."""
import pytest
from pydantic import ValidationError

from specpilot.dummies import DummyFile, build
from specpilot.tools import gold
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.value_finder import make_people
from tests.test_branch_maker import custom

SET = make_branches(gold.load("REQ-41-PRECONDITION"), edges=True)
SEED = 4711


@pytest.fixture(scope="module")
def people():
    return make_people(SET, SEED)


@pytest.fixture(scope="module")
def dummy(people):
    return build(people, SET, "test")


# --- building -------------------------------------------------------------------------------------------

def test_one_row_per_person_and_per_branch(dummy):
    assert len(dummy.people) == 34 and len(dummy.coverage) == 34
    assert all(c.outcome == "person" for c in dummy.coverage)
    assert sorted(p.neutral_id for p in dummy.people) == [f"DUMMY-{n:04d}" for n in range(1, 35)]
    assert all(p.provenance == "SYNTHETIC" and p.seed == SEED and p.card_hash == SET.card_hash for p in dummy.people)


def test_truth_labels_are_the_branch_design(dummy):
    patterns = {b.branch_id: b.pattern for b in SET.branches}
    for p in dummy.people:
        assert "".join("✔" if v else "✘" for v in p.truth.values()) == patterns[p.branch_id]
    assert [p.all_true for p in dummy.people if p.all_true] == [1, 1]  # B01 and its edge


def test_edge_labels(dummy):
    edges = {p.person_id: (p.edge_criterion, p.edge_value) for p in dummy.people if p.edge_criterion}
    assert edges == {"REQ-41-PRECONDITION-B01-E1-P1": ("C3", 30), "REQ-41-PRECONDITION-B05-E1-P1": ("C3", 29)}


def test_raw_values_rebuild_the_exact_person(dummy, people):
    for row, p in zip(dummy.people, people.people):
        assert dummy.scenario_of(row) == p.scenario


def test_earlier_periods_are_numbered_oldest_first(dummy, people):
    assert len(dummy.previous) == sum(len(p.scenario.previous) for p in people.people)
    for p in people.people:
        mine = [q for q in dummy.previous if q.person_id == p.person_id]
        assert [q.number for q in mine] == list(range(1, len(p.scenario.previous) + 1))
        assert [q.start for q in mine] == sorted(q.start for q in mine)


def test_impossible_branches_appear_in_the_coverage():
    s = make_branches(custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1)))
    d = build(make_people(s, SEED), s, "test")
    assert [c.outcome for c in d.coverage].count("impossible") == 1 and len(d.people) == 3
    assert "proved" in next(c.reason for c in d.coverage if c.outcome == "impossible")


def test_people_from_another_card_version_are_refused(people):
    other = make_branches(gold.load("REQ-41-PRECONDITION").model_copy(update={"version": 2}), edges=True)
    with pytest.raises(ValueError, match="another card version"):
        build(people, other, "test")


def test_round_trip(dummy):
    assert DummyFile.model_validate_json(dummy.model_dump_json()) == dummy


# --- broken files are refused --------------------------------------------------------------------------------

def broken(dummy, change):
    data = dummy.model_dump(mode="json")
    change(data)
    return data


@pytest.mark.parametrize("change, message", [
    (lambda d: d["people"].pop(5), "people and coverage disagree"),
    (lambda d: d["coverage"].append(d["coverage"][0]), "appears twice in the coverage"),
    (lambda d: d["people"][3].update(neutral_id="DUMMY-0099"), "exactly DUMMY-0001"),
    (lambda d: d["people"][3].update(seed=1), "another card version or seed"),
    (lambda d: d["people"][3].update(all_true=1), "all_true does not follow"),
    (lambda d: d["people"][3]["truth"].pop("C5"), "truth labels, the card has 5"),
    (lambda d: d["people"][3]["truth"].update(C2=2), "are 0 or 1"),
    (lambda d: d["people"][3].update(edge_value=30), "go together"),
    (lambda d: d["people"][3].update(current_end="2027-01-01", current_start="2027-02-01"), "ends before it starts"),
    (lambda d: d["people"][3].update(provenance="REAL"), "provenance"),
    (lambda d: d["previous"][0].update(number=3), "numbered 1, 2"),
    (lambda d: d["previous"][0].update(person_id="REQ-41-X-B01-P1"), "unknown person"),
    (lambda d: d["coverage"][0].update(reason="should not be here"), "a reason exactly when"),
    (lambda d: d["people"][3].update(extra=1), "Extra inputs"),
])
def test_broken_dummy_files_are_refused(dummy, change, message):
    with pytest.raises(ValidationError, match=message):
        DummyFile.model_validate(broken(dummy, change))


def test_neutral_ids_do_not_follow_the_branch_order(dummy):
    """In branch order DUMMY-0001 would always be the all-met person: the ids are shuffled with the seed."""
    order = [int(p.neutral_id[6:]) for p in dummy.people]
    assert order != sorted(order)
    assert dummy.people[0].neutral_id != "DUMMY-0001"


def test_neutral_ids_are_reproducible(people):
    assert [p.neutral_id for p in build(people, SET, "x").people] == [p.neutral_id for p in build(people, SET, "y").people]
