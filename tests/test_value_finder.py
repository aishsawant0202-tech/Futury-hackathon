"""Value finder: a person for every possible branch, proofs for impossible ones, never confusing the two."""
import datetime as dt

import pytest
import z3

from specpilot.branches import Target
from specpilot.tools import facts, gold
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.value_finder import FinderBug, check_person, solve_all, solve_branch
from tests.test_branch_maker import custom

GOLD = gold.load("REQ-41-PRECONDITION")
START = dt.date(2027, 3, 14)


def reached(branch, person) -> bool:
    return check_person(branch, person) == []


# --- the reason 41 card ------------------------------------------------------------------------------

def test_every_branch_of_the_reason_41_card_gets_a_person():
    s = make_branches(GOLD, edges=True)
    results = solve_all(s, START)
    assert [r.branch_id for r in results] == [b.branch_id for b in s.branches]
    assert all(r.outcome == "found" for r in results)
    for b, r in zip(s.branches, results):
        assert reached(b, r.person), b.branch_id
        assert r.person.current.start == START


def test_people_really_meet_and_fail_what_their_branch_says():
    """Independent of check_person: recompute every condition with the fact dictionary."""
    s = make_branches(GOLD)
    for b, r in zip(s.branches, solve_all(s, START)):
        values = facts.evaluate(r.person)
        for t in b.targets:
            assert facts.holds(values[t.fact], t.op, t.value) is (t.target is Target.MET), (b.branch_id, t.criterion_id)


def test_edge_people_sit_exactly_at_the_limit():
    s = make_branches(GOLD, edges=True)
    edges = {r.branch_id: facts.evaluate(r.person)["total_days_12m"] for r in solve_all(s, START) if "-E" in r.branch_id}
    assert edges == {"REQ-41-PRECONDITION-B01-E1": 30, "REQ-41-PRECONDITION-B05-E1": 29}


@pytest.mark.parametrize("start", [dt.date(2027, 1, 1), dt.date(2027, 2, 28), dt.date(2027, 3, 31),
                                   dt.date(2027, 8, 31), dt.date(2027, 12, 31)])
def test_any_start_date_in_2027(start):
    """Month ends and year ends make the 6- and 12-month windows awkward: still a person for every branch."""
    s = make_branches(GOLD, edges=True)
    results = solve_all(s, start)
    assert all(r.outcome == "found" and reached(b, r.person) for b, r in zip(s.branches, results))


def people_json(start: dt.date) -> str:
    return "\n".join(r.person.model_dump_json() for r in solve_all(make_branches(GOLD, edges=True), start))


def test_same_start_same_people_even_after_other_puzzles():
    first = people_json(START)
    solve_all(make_branches(custom(("total_days_12m", ">=", 30), ("current_open_end", "=", True))), dt.date(2027, 9, 1))
    assert people_json(START) == first


def test_same_people_in_a_separate_process():
    import subprocess
    import sys
    code = ("import datetime as dt; from tests.test_value_finder import people_json; "
            "import sys; sys.stdout.write(people_json(dt.date(2027, 3, 14)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out == people_json(START)


# --- impossible across facts (Station 3 cannot see these) ----------------------------------------------------

def outcome(card, pattern: str):
    s = make_branches(card)
    b = next(b for b in s.branches if b.pattern == pattern)
    assert b.status.value == "possible"  # the branch maker could not tell
    return solve_branch(b, START)


def test_illness_in_6_months_needs_at_least_2_days():
    """An earlier certified illness (>= 1 day) plus the current period (>= 1 day) is at least 2 days."""
    card = custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1))
    r = outcome(card, "✔✔")
    assert r.outcome == "impossible" and r.person is None
    assert r.reason.startswith("(value finder) proved: no realistic person with 0-4 earlier sick periods")
    assert outcome(card, "✔✘").outcome == "found"


def test_open_end_needs_at_least_8_days():
    """Open end counts to report date + 7, so the current period alone is >= 8 days."""
    card = custom(("current_open_end", "=", True), ("total_days_12m", "<", 8))
    assert outcome(card, "✔✔").outcome == "impossible"
    assert outcome(card, "✔✘").outcome == "found" and outcome(card, "✘✔").outcome == "found"


def test_branch_maker_impossible_branches_pass_through():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 20)))
    b = next(b for b in s.branches if b.pattern == "✔✔")
    r = solve_branch(b, START)
    assert r.outcome == "impossible" and r.reason.startswith("(branch maker)")


# --- giving up is not impossible -------------------------------------------------------------------------------

def test_timeout_is_unsolved_not_impossible(monkeypatch):
    monkeypatch.setattr(z3.Solver, "check", lambda self, *a: z3.unknown)
    r = solve_branch(make_branches(GOLD).branches[0], START)
    assert r.outcome == "unsolved" and r.person is None
    assert "NOT a proof" in r.reason


def test_timeout_for_some_counts_still_finds_with_others(monkeypatch):
    real = z3.Solver.check
    calls = {"n": 0}

    def flaky(self, *a):
        calls["n"] += 1
        return z3.unknown if calls["n"] == 1 else real(self, *a)

    monkeypatch.setattr(z3.Solver, "check", flaky)
    r = solve_branch(make_branches(GOLD).branches[0], START)  # needs at least 1 earlier period anyway
    assert r.outcome == "found"


# --- the built-in recheck ----------------------------------------------------------------------------------------

def test_a_wrong_person_stops_the_run(monkeypatch):
    """If the date model and the fact dictionary ever disagreed, the finder must stop, not hand on bad data."""
    from specpilot.tools import value_finder
    real = value_finder.to_scenario

    def broken(v, m):
        p = real(v, m)
        return p.model_copy(update={"statutory_insured": not p.statutory_insured})

    monkeypatch.setattr(value_finder, "to_scenario", broken)
    with pytest.raises(FinderBug, match="C4 statutory_insured = true should be met"):
        solve_branch(make_branches(GOLD).branches[0], START)


def test_check_person_reports_every_miss():
    b = make_branches(GOLD, edges=True).branches[1]  # B01-E1: total exactly 30
    person = solve_branch(make_branches(GOLD).branches[-1], START).person  # B32: everything failed
    wrong = check_person(b, person)
    assert len(wrong) >= 4 and all("should be met" in w or "edge" in w for w in wrong)
