"""Variety + seed: realistic, varied people in 2027, reproducible from the seed, never at the cost of correctness."""
import datetime as dt
import statistics

import pytest
import z3

from specpilot.branches import Target
from specpilot.tools import facts, gold
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.value_finder import (LAST_DAY, check_person, draw_wishes, find_people, make_rng, solve_varied,
                                          wished_number)
from tests.test_branch_maker import custom

GOLD = gold.load("REQ-41-PRECONDITION")
SET = make_branches(GOLD, edges=True)
SEED = 4711


@pytest.fixture(scope="module")
def run():
    """One full run (about 30 s), shared by the tests below."""
    return find_people(SET, SEED)


# --- correctness is untouched ----------------------------------------------------------------------

def test_every_branch_gets_a_correct_person(run):
    assert all(r.outcome == "found" for r in run)
    for b, r in zip(SET.branches, run):
        assert check_person(b, r.person) == [], b.branch_id


def test_edge_people_still_sit_exactly_at_the_limit(run):
    edges = {r.branch_id: facts.evaluate(r.person)["total_days_12m"] for r in run if "-E" in r.branch_id}
    assert edges == {"REQ-41-PRECONDITION-B01-E1": 30, "REQ-41-PRECONDITION-B05-E1": 29}


def test_cross_fact_impossible_is_still_proved():
    s = make_branches(custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1)))
    r = solve_varied(next(b for b in s.branches if b.pattern == "✔✔"), SEED)
    assert r.outcome == "impossible" and "proved" in r.reason


# --- 2027 -------------------------------------------------------------------------------------------------

def test_current_period_and_report_date_lie_in_2027(run):
    for r in run:
        p = r.person
        assert p.current.start.year == p.current.end.year == p.report_date.year == 2027, r.branch_id
        assert p.current.end <= LAST_DAY and p.report_date <= LAST_DAY


def test_dates_stay_in_2027_even_when_the_wishes_run_into_2028(monkeypatch):
    """A 60-day illness wished from 15.11.2027 would end in January 2028: the year rule must win."""
    from specpilot.tools import value_finder
    real = value_finder.draw_wishes

    def late(rng, branch=None):
        w = real(rng, branch)
        w.start, w.current_len, w.report_offset = dt.date(2027, 11, 15), 60, 40
        return w

    monkeypatch.setattr(value_finder, "draw_wishes", late)
    for b in SET.branches[:4]:
        r = solve_varied(b, SEED)
        assert r.outcome == "found" and check_person(b, r.person) == []
        assert r.person.current.end <= LAST_DAY and r.person.report_date <= LAST_DAY


def test_year_rule_beats_wishes_that_fit_everything_else():
    """Only a sick note is required and the wishes fit perfectly - except that they end in January 2028."""
    from specpilot.tools import value_finder
    from specpilot.tools.value_finder import Wishes
    b = make_branches(custom(("current_certified", "=", True))).branches[0]
    w = Wishes(start=dt.date(2027, 11, 15), n_previous=0, current_len=60, report_offset=40, previous=[],
               yes_no={"current_certified": True, "open_end": False, "statutory": True, "marginal": False,
                       **{f"prev{i}_certified": True for i in range(4)}}, total_days=60)
    original = value_finder.draw_wishes
    value_finder.draw_wishes = lambda rng, branch=None: w
    try:
        r = solve_varied(b, SEED)
    finally:
        value_finder.draw_wishes = original
    assert r.outcome == "found"
    assert r.person.current.end <= LAST_DAY and r.person.report_date <= LAST_DAY
    assert r.person.current.end >= dt.date(2027, 12, 20)  # as close to the wish as 2027 allows


# --- variety -------------------------------------------------------------------------------------------------

def test_people_are_varied(run):
    people = [r.person for r in run]
    values = [facts.evaluate(p) for p in people]
    assert len({p.current.start.month for p in people}) >= 8  # spread over the year
    assert len({v["total_days_12m"] for v in values}) >= 20  # not always the same totals
    assert statistics.median((p.current.end - p.current.start).days + 1 for p in people) >= 5  # not 1-day illnesses
    assert len({len(p.previous) for p in people}) >= 3  # different numbers of earlier illnesses
    assert {p.open_end for p in people} == {True, False}  # free fact takes both values


def test_ordinary_people_stay_away_from_the_limit(run):
    """Only the edge branches may sit within 2 days of the 30-day limit."""
    near = [r.branch_id for r in run if "-E" not in r.branch_id
            and abs(facts.evaluate(r.person)["total_days_12m"] - 30) <= 2]
    assert near == []


def test_wished_number_lies_on_the_right_side_away_from_the_limit():
    b = SET.branches[0]  # C3 total_days_12m >= 30 must be MET
    assert all(wished_number(b, "total_days_12m", make_rng(s, "x")) >= 33 for s in range(50))
    failed = next(x for x in SET.branches if x.branch_id.endswith("B05"))  # C3 must FAIL
    assert all(wished_number(failed, "total_days_12m", make_rng(s, "x")) <= 26 for s in range(50))
    assert wished_number(b, "current_certified", make_rng(1, "x")) is None


def test_between_wish_stays_inside():
    s = make_branches(custom(("total_days_12m", "between", [30, 60])))
    met = s.branches[0]
    assert all(33 <= wished_number(met, "total_days_12m", make_rng(i, "x")) <= 57 for i in range(50))


# --- seed -----------------------------------------------------------------------------------------------------

def test_same_seed_same_person():
    b = SET.branches[3]
    assert solve_varied(b, SEED).person == solve_varied(b, SEED).person


def test_person_depends_only_on_seed_and_branch(run):
    """Solving one branch alone gives the same person as in the full run."""
    for i in (0, 9, 20):
        assert solve_varied(SET.branches[i], SEED).person == run[i].person


def test_different_seeds_give_different_people(run):
    other = [solve_varied(b, SEED + 1).person for b in SET.branches[:8]]
    assert sum(a != b.person for a, b in zip(other, run[:8])) >= 7


def test_same_people_in_a_separate_process(run):
    import subprocess
    import sys
    code = ("import sys; from tests.test_variety import SET, SEED; from specpilot.tools.value_finder import solve_varied; "
            "sys.stdout.write('\\n'.join(solve_varied(SET.branches[i], SEED).person.model_dump_json() for i in (0, 9)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out == "\n".join(run[i].person.model_dump_json() for i in (0, 9))


def test_wishes_are_drawn_from_the_seed_alone():
    a = draw_wishes(make_rng(SEED, "B01"), SET.branches[0])
    assert a == draw_wishes(make_rng(SEED, "B01"), SET.branches[0])
    assert a != draw_wishes(make_rng(SEED, "B02"), SET.branches[0])
    assert dt.date(2027, 1, 1) <= a.start <= dt.date(2027, 11, 15)


# --- falling back -----------------------------------------------------------------------------------------------

def test_solver_giving_up_on_the_wishes_falls_back_to_a_valid_person(monkeypatch):
    """The first 40 questions time out (all wish rungs); the plain solver still finds a valid person."""
    real = z3.Solver.check
    calls = {"n": 0}

    def flaky(self, *a):
        calls["n"] += 1
        return z3.unknown if calls["n"] <= 40 else real(self, *a)

    monkeypatch.setattr(z3.Solver, "check", flaky)
    b = SET.branches[0]
    r = solve_varied(b, SEED)
    assert r.outcome == "found" and check_person(b, r.person) == []


def test_solver_giving_up_everywhere_is_unsolved_not_impossible(monkeypatch):
    monkeypatch.setattr(z3.Solver, "check", lambda self, *a: z3.unknown)
    r = solve_varied(SET.branches[0], SEED)
    assert r.outcome == "unsolved" and "NOT a proof" in r.reason


def test_full_run_is_fast_enough(run):
    import time
    start = time.time()
    find_people(SET, SEED + 7)
    assert time.time() - start < 30  # 34 people take about 2 s
