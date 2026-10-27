"""Self-check: passes correct dummy data, and catches every kind of wrong data with a clear message."""
import datetime as dt

import pytest

from specpilot.criteria import CriteriaCard
from specpilot.dummies import build
from specpilot.review import approve
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from tests.test_branch_maker import custom
from tests.test_review import RULES, draft

D = dt.timedelta


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def branches(card):
    return make_branches(card, edges=True)


@pytest.fixture(scope="module")
def dummy(branches):
    return build(make_people(branches, 4711), branches, "test")


def checks(report) -> list[tuple[str, str]]:
    return [(p.check, p.where) for p in report.problems]


def with_person(d, i, **changes):
    """The dummy file with person i changed, WITHOUT the form's checks (as if the data were corrupted)."""
    people = list(d.people)
    people[i] = people[i].model_copy(update=changes)
    return d.model_copy(update={"people": people})


def with_previous(d, pid, n, **changes):
    prev = [q.model_copy(update=changes) if (q.person_id, q.number) == (pid, n) else q for q in d.previous]
    return d.model_copy(update={"previous": prev})


# --- correct data passes ---------------------------------------------------------------------------------

def test_correct_dummy_file_passes(card, branches, dummy):
    r = check(card, branches, dummy)
    assert r.ok and r.people_checked == 34 and r.branches == 34 and not r.notes
    assert r.text().startswith("SELF-CHECK PASSED: 34 people checked against 34 branches, 0 problem(s)")


@pytest.mark.parametrize("seed", [1, 2027])
def test_other_seeds_pass_too(card, branches, seed):
    assert check(card, branches, build(make_people(branches, seed), branches, "test")).ok


# --- wrong people -------------------------------------------------------------------------------------------

def test_raw_value_that_breaks_a_condition(card, branches, dummy):
    r = check(card, branches, with_person(dummy, 0, statutory_insured=False))
    assert ("conditions", "REQ-41-PRECONDITION-B01-P1") in checks(r) and not r.ok
    assert "C4 statutory_insured = true should be met, but from the raw values statutory_insured = False" in r.text()


def test_dates_that_break_the_30_days(card, branches, dummy):
    """B01 must meet >= 30 days: shrink its earlier illnesses to one day each."""
    pid = dummy.people[0].person_id
    d = dummy
    for q in [q for q in dummy.previous if q.person_id == pid]:
        d = with_previous(d, pid, q.number, end=q.start)
    d = with_person(d, 0, current_end=d.people[0].current_start)
    r = check(card, branches, d)
    assert any("C3 total_days_12m >= 30 should be met" in p.message for p in r.problems)


def test_wrong_truth_labels(card, branches, dummy):
    labels = dict(dummy.people[2].truth, C1=0)
    r = check(card, branches, with_person(dummy, 2, truth=labels, all_true=0))
    assert ("labels", dummy.people[2].person_id) in checks(r)


def test_edge_person_one_day_off(card, branches, dummy):
    i = next(i for i, p in enumerate(dummy.people) if p.edge_value == 29)
    p = dummy.people[i]
    # an open end is counted up to the report date + 7, otherwise up to the end date: move the one that counts
    change = {"report_date": p.report_date + D(days=1)} if p.open_end else {"current_end": p.current_end + D(days=1)}
    r = check(card, branches, with_person(dummy, i, **change))
    assert any(x.check == "edge" and "exactly at 29" in x.message for x in r.problems)


def test_edge_labels_that_differ_from_the_branch(card, branches, dummy):
    i = next(i for i, p in enumerate(dummy.people) if p.edge_value == 30)
    r = check(card, branches, with_person(dummy, i, edge_criterion="", edge_value=None))
    assert ("edge", dummy.people[i].person_id) in checks(r)


# --- realism ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    (lambda p: {"current_start": dt.date(2026, 12, 30)}, "is not in 2027"),
    (lambda p: {"report_date": p.current_start + D(days=41)}, "allowed 0-40"),
    (lambda p: {"report_date": p.current_start - D(days=1)}, "allowed 0-40"),
    (lambda p: {"current_end": p.current_start + D(days=60)}, "61 days (allowed 1-60)"),
])
def test_unrealistic_current_period(card, branches, dummy, change, message):
    p = dummy.people[5]
    r = check(card, branches, with_person(dummy, 5, **change(p)))
    assert any(x.check == "realism" and message in x.message for x in r.problems), r.text()


def test_unrealistic_earlier_periods(card, branches, dummy):
    p = next(p for p in dummy.people if sum(q.person_id == p.person_id for q in dummy.previous) >= 2)
    first, second = [q for q in dummy.previous if q.person_id == p.person_id][:2]
    touching = with_previous(dummy, p.person_id, 1, end=second.start - D(days=1))
    assert any("without a free day" in x.message for x in check(card, branches, touching).problems)
    too_long = with_previous(dummy, p.person_id, 1, start=first.end - D(days=60))
    assert any("61 days" in x.message for x in check(card, branches, too_long).problems)


def test_duplicate_people(card, branches, dummy):
    """Two branches cannot share one person: the copy also breaks its own branch's conditions."""
    src = dummy.people[0]
    raw = {k: getattr(src, k) for k in ("report_date", "current_start", "current_end", "current_certified", "open_end",
                                        "statutory_insured", "marginal_employment")}
    d = with_person(dummy, 3, **raw)
    pid = d.people[3].person_id
    prev = [q for q in d.previous if q.person_id != pid] + [
        q.model_copy(update={"person_id": pid}) for q in d.previous if q.person_id == src.person_id]
    r = check(card, branches, d.model_copy(update={"previous": prev}))
    assert ("duplicate", pid) in checks(r) and ("conditions", pid) in checks(r)


# --- card, branches, coverage -------------------------------------------------------------------------------------

def test_card_not_approved_or_changed(branches, dummy):
    r = check(draft(), branches, dummy)
    assert ("card", "card") in checks(r)
    a = approve(draft(), "A", RULES)
    edited = a.model_copy(update={"title": "changed after approval"})
    r = check(edited, branches, dummy)
    assert any("changed after approval" in p.message for p in r.problems)


def test_branches_from_another_card_version(card, dummy):
    other = CriteriaCard.model_validate(card.model_dump() | {"version": 2, "status": "draft", "reviewer": "",
                                                             "reviewed_at": None, "approved_hash": ""})
    other = approve(other, "A", RULES)
    r = check(other, make_branches(card, edges=True), dummy)
    assert checks(r) == [("branches", "branches")] and "fingerprints differ" in r.text()


def test_branch_conditions_changed_but_fingerprint_copied(card, branches, dummy):
    b = branches.branches[0]
    t = b.targets[2].model_copy(update={"value": 31})
    forged = branches.model_copy(update={"branches": [b.model_copy(update={"targets": [*b.targets[:2], t, *b.targets[3:]]}),
                                                      *branches.branches[1:]]})
    r = check(card, forged, dummy)
    assert any(p.check == "branches" and "C3 says total_days_12m >= 31, the card says total_days_12m >= 30" in p.message
               for p in r.problems)


def test_missing_and_unsolved_branches(card, branches, dummy):
    r = check(card, branches, dummy.model_copy(update={"coverage": dummy.coverage[:-1]}))
    assert any("branches missing ['REQ-41-PRECONDITION-B32']" in p.message for p in r.problems)
    cov = list(dummy.coverage)
    cov[4] = cov[4].model_copy(update={"outcome": "unsolved", "person_id": "", "reason": "timeout"})
    r = check(card, branches, dummy.model_copy(update={"coverage": cov}))
    assert ("coverage", cov[4].branch_id) in checks(r)


def test_solver_proofs_are_listed_as_notes():
    from specpilot.criteria import CriteriaCard as Card
    c = custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1))
    approved = Card.model_validate(c.model_dump() | {"status": "approved", "reviewer": "A",
                                                     "reviewed_at": dt.datetime(2027, 1, 1),
                                                     "approved_hash": c.content_hash()})
    s = make_branches(approved)
    r = check(approved, s, build(make_people(s, 1), s, "test"))
    assert r.ok and len(r.notes) == 1 and "proven impossible by the value finder's solver" in r.notes[0]


def test_every_problem_is_reported_not_just_the_first(card, branches, dummy):
    d = with_person(with_person(dummy, 0, statutory_insured=False), 7, current_start=dt.date(2026, 6, 1))
    r = check(card, branches, d)
    assert {p.where for p in r.problems} >= {dummy.people[0].person_id, dummy.people[7].person_id}
