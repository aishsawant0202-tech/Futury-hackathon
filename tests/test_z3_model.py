"""Date model: the Z3 formulas give exactly the same facts as the fact dictionary, for any realistic person."""
import datetime as dt
import random

import pytest
import z3

from specpilot.models import AbsencePeriod, Scenario
from specpilot.tools import facts
from specpilot.tools.z3_model import (LOOKBACK_DAYS, MAX_LEN, MAX_PREVIOUS, REPORT_WITHIN, condition, fact_exprs,
                                      person_vars, shape, to_scenario)

D = dt.timedelta


def random_person(rng: random.Random) -> Scenario:
    """A concrete person that follows the shape rules, built without Z3."""
    start = dt.date(2027, 1, 1) + D(days=rng.randint(0, 364))
    cur_end = start + D(days=rng.randint(0, MAX_LEN - 1))
    previous, cursor = [], start - D(days=2)  # build backwards: each period ends >= 2 days before the next starts
    for _ in range(rng.randint(0, MAX_PREVIOUS)):
        end = cursor - D(days=rng.randint(0, 120))
        begin = end - D(days=rng.randint(0, MAX_LEN - 1))
        if begin < start - D(days=LOOKBACK_DAYS):
            break
        previous.insert(0, AbsencePeriod(start=begin, end=end, certified=rng.random() < 0.75))
        cursor = begin - D(days=2)
    return Scenario(report_date=start + D(days=rng.randint(0, REPORT_WITHIN)),
                    current=AbsencePeriod(start=start, end=cur_end, certified=rng.random() < 0.85),
                    previous=previous, statutory_insured=rng.random() < 0.85,
                    marginal_employment=rng.random() < 0.15, open_end=rng.random() < 0.25)


def pin(v, p: Scenario) -> list:
    """Z3 constraints that fix every unknown of `v` to the concrete person `p`."""
    out = [v.current_end == p.current.end.toordinal(), v.report == p.report_date.toordinal(),
           v.current_certified == p.current.certified, v.open_end == p.open_end,
           v.statutory == p.statutory_insured, v.marginal == p.marginal_employment]
    for var, per in zip(v.previous, p.previous):
        out += [var.start == per.start.toordinal(), var.end == per.end.toordinal(), var.certified == per.certified]
    return out


def values_of(v, p: Scenario) -> list[tuple]:
    """(unknown, concrete value) pairs: plugging the person into the formulas, without running a solver."""
    out = [(v.current_end, z3.IntVal(p.current.end.toordinal())), (v.report, z3.IntVal(p.report_date.toordinal())),
           (v.current_certified, z3.BoolVal(p.current.certified)), (v.open_end, z3.BoolVal(p.open_end)),
           (v.statutory, z3.BoolVal(p.statutory_insured)), (v.marginal, z3.BoolVal(p.marginal_employment))]
    for var, per in zip(v.previous, p.previous):
        out += [(var.start, z3.IntVal(per.start.toordinal())), (var.end, z3.IntVal(per.end.toordinal())),
                (var.certified, z3.BoolVal(per.certified))]
    return out


def z3_facts(p: Scenario) -> dict:
    v = person_vars(p.current.start, len(p.previous))
    pairs = values_of(v, p)
    assert z3.is_true(z3.simplify(z3.substitute(z3.And(shape(v)), *pairs))), "the person breaks the shape rules"
    out = {}
    for name, e in fact_exprs(v).items():
        val = z3.simplify(z3.substitute(e, *pairs))
        out[name] = z3.is_true(val) if facts.FACTS[name].type.value == "boolean" else val.as_long()
    return out


# --- the formulas agree with the fact dictionary ----------------------------------------------------

def test_formulas_agree_with_the_fact_dictionary_on_random_people():
    rng = random.Random(2027)
    seen = {name: set() for name in facts.FACTS}
    for _ in range(600):
        p = random_person(rng)
        assert z3_facts(p) == facts.evaluate(p), p
        for name, value in facts.evaluate(p).items():
            seen[name].add(value)
    # the sample must really exercise every fact, or the test proves nothing
    # (an exact 6-month link is too rare for random people: test_chain_link_exactly_six_months places people on it)
    for name in facts.FACTS:
        if name != "chain_link_exactly_6m":
            assert len(seen[name]) >= 2, name
    assert any(abs(x - 30) <= 1 for x in seen["total_days_12m"])


@pytest.mark.parametrize("label, prev_end_days_before_window", [("inside", 0), ("outside", 1)])
def test_six_month_window_edge(label, prev_end_days_before_window):
    start = dt.date(2027, 3, 14)  # 6 months before: 14.09.2026 - 13.03.2027
    end = dt.date(2026, 9, 14) - D(days=prev_end_days_before_window)
    p = Scenario(report_date=start, current=AbsencePeriod(start=start, end=start),
                 previous=[AbsencePeriod(start=end - D(days=3), end=end)])
    assert z3_facts(p)["prior_certified_6m"] == facts.evaluate(p)["prior_certified_6m"] == (label == "inside")


def test_twelve_month_window_cuts_a_period():
    start = dt.date(2027, 3, 14)  # 12 months before start on 14.03.2026
    p = Scenario(report_date=start, current=AbsencePeriod(start=start, end=start + D(days=9)),
                 previous=[AbsencePeriod(start=dt.date(2026, 3, 5), end=dt.date(2026, 3, 20))])
    assert z3_facts(p)["total_days_12m"] == facts.evaluate(p)["total_days_12m"] == 7 + 10


def test_open_end_counts_to_report_date_plus_7():
    start = dt.date(2027, 5, 3)
    p = Scenario(report_date=start + D(days=4), current=AbsencePeriod(start=start, end=start), open_end=True)
    assert z3_facts(p)["total_days_12m"] == facts.evaluate(p)["total_days_12m"] == 12


def test_uncertified_periods_do_not_count():
    start = dt.date(2027, 6, 1)
    p = Scenario(report_date=start, current=AbsencePeriod(start=start, end=start + D(days=4)),
                 previous=[AbsencePeriod(start=dt.date(2027, 4, 1), end=dt.date(2027, 4, 30), certified=False)])
    assert z3_facts(p) == facts.evaluate(p)
    assert z3_facts(p)["total_days_12m"] == 5 and not z3_facts(p)["prior_certified_6m"]


# --- solving: Z3 finds people, and they really have the facts asked for -----------------------------------

@pytest.mark.parametrize("wish", [
    {"prior_certified_6m": True, "total_days_12m": (">=", 30)},
    {"prior_certified_6m": True, "total_days_12m": ("<", 30)},
    {"prior_certified_6m": False, "total_days_12m": (">=", 30)},
    {"prior_certified_6m": False, "total_days_12m": ("=", 29)},
    {"current_open_end": True, "total_days_12m": ("=", 30)},
])
def test_found_people_have_the_wished_facts(wish):
    v = person_vars(dt.date(2027, 3, 14), 3)
    s = z3.Solver()
    s.add(shape(v))
    e = fact_exprs(v)
    for name, want in wish.items():
        s.add(condition(e[name], *want) if isinstance(want, tuple) else e[name] == want)
    assert s.check() == z3.sat
    person = to_scenario(v, s.model())
    got = facts.evaluate(person)
    for name, want in wish.items():
        assert facts.holds(got[name], *want) if isinstance(want, tuple) else got[name] == want, (name, got)


def test_impossible_wish_is_proved_impossible():
    """No earlier periods means no earlier illness in the last 6 months: Z3 must say so."""
    v = person_vars(dt.date(2027, 3, 14), 0)
    s = z3.Solver()
    s.add(shape(v) + [fact_exprs(v)["prior_certified_6m"]])
    assert s.check() == z3.unsat


def test_shape_rules_refuse_unrealistic_people():
    start = dt.date(2027, 3, 14)
    v = person_vars(start, 2)
    s = z3.Solver()
    s.add(shape(v))
    touching = [v.previous[0].end + 1 == v.previous[1].start]
    too_long = [v.current_end - start.toordinal() + 1 == MAX_LEN + 1]
    after = [v.previous[1].end >= start.toordinal() - 1]
    for bad in (touching, too_long, after):
        s.push()
        s.add(bad)
        assert s.check() == z3.unsat
        s.pop()


@pytest.mark.parametrize("op, value, inside, outside", [
    ("=", 30, 30, 29), ("!=", 30, 29, 30), (">", 30, 31, 30), (">=", 30, 30, 29),
    ("<", 30, 29, 30), ("<=", 30, 30, 31), ("between", [30, 60], 45, 61),
])
def test_condition_operators_match_facts_holds(op, value, inside, outside):
    x = z3.Int("x")
    for v, expect in ((inside, True), (outside, False)):
        assert z3.is_true(z3.simplify(z3.substitute(condition(x, op, value), (x, z3.IntVal(v))))) is expect
        assert facts.holds(v, op, value) is expect


def test_every_fact_needs_a_formula(monkeypatch):
    monkeypatch.setitem(facts.FACTS, "shoe_size", facts.FACTS["total_days_12m"])
    with pytest.raises(NotImplementedError, match="shoe_size"):
        fact_exprs(person_vars(dt.date(2027, 1, 10), 1))


def test_round_trip_to_scenario():
    p = random_person(random.Random(5))
    v = person_vars(p.current.start, len(p.previous))
    s = z3.Solver()
    s.add(shape(v) + pin(v, p))
    assert s.check() == z3.sat
    assert to_scenario(v, s.model()) == p



# --- chain facts on and around their edges ----------------------------------------------------------------------

@pytest.mark.parametrize("start", [dt.date(2027, 3, 14), dt.date(2027, 2, 28), dt.date(2027, 8, 31),
                                   dt.date(2027, 3, 1), dt.date(2027, 12, 31)])
def test_latest_gap_edge(start):
    """Latest earlier illness ending on every day around the 6-month edge: Z3 and the dictionary agree."""
    from specpilot.tools.z3_model import gap_end_days
    within_from, _ = gap_end_days(start)
    seen = set()
    for end in range(within_from - 4, within_from + 5):
        e = dt.date.fromordinal(end)
        for certified in (True, False):
            p = Scenario(report_date=start, current=AbsencePeriod(start=start, end=start),
                         previous=[AbsencePeriod(start=e - D(days=3), end=e, certified=certified)])
            assert z3_facts(p) == facts.evaluate(p), (start, e, certified)
            seen.add(facts.evaluate(p)["latest_gap_within_6m"])
    assert seen == {True, False}


@pytest.mark.parametrize("start", [dt.date(2027, 3, 14), dt.date(2027, 2, 28), dt.date(2027, 8, 31),
                                   dt.date(2027, 12, 31)])
def test_chain_link_exactly_six_months(start):
    """Illness A, then illness B starting on every day around 'exactly 6 months after A': Z3 and the
    dictionary agree, and only the exact day counts. Also with an absence without a sick note in between,
    which the chain skips."""
    from specpilot.tools.z3_model import exact_link_pairs
    P = AbsencePeriod
    pairs = list(exact_link_pairs(start))
    exact_seen = 0
    for a_end, b_start in pairs[::40] + [pairs[-1]]:
        a_end_d = dt.date.fromordinal(a_end)
        if a_end_d - D(days=4) < start - D(days=LOOKBACK_DAYS):
            continue  # illness A would start further back than the realism rules allow
        for shift in (-2, -1, 0, 1, 2):
            b0 = dt.date.fromordinal(b_start + shift)
            if b0 + D(days=2) >= start - D(days=1) or b0 <= a_end_d + D(days=1):
                continue
            for middle in (False, True):
                prev = [P(start=a_end_d - D(days=4), end=a_end_d)]
                if middle and (b0 - a_end_d).days > 6:
                    prev.append(P(start=a_end_d + D(days=2), end=a_end_d + D(days=3), certified=False))
                prev.append(P(start=b0, end=b0 + D(days=1)))
                p = Scenario(report_date=start, current=P(start=start, end=start), previous=prev)
                v = facts.evaluate(p)
                assert z3_facts(p) == v, (start, a_end_d, b0, middle)
                assert v["chain_link_exactly_6m"] is (shift == 0 or any(
                    b == b_start + shift and a == a_end for a, b in pairs)), (a_end_d, b0)
                exact_seen += v["chain_link_exactly_6m"]
    assert exact_seen > 0


def test_exactly_six_months_exists_or_not():
    """Month-end clamping: some start dates have several 'exactly 6 months' end days, some have none."""
    from specpilot.tools.z3_model import gap_end_days
    assert gap_end_days(dt.date(2027, 2, 28))[1] == (dt.date(2026, 8, 27).toordinal(), dt.date(2026, 8, 30).toordinal())
    assert gap_end_days(dt.date(2027, 8, 31))[1] is None
    assert gap_end_days(dt.date(2027, 3, 14))[1] == (dt.date(2026, 9, 13).toordinal(),) * 2


def test_chain_facts_by_hand():
    start = dt.date(2027, 6, 1)  # 12 months before: from 01.06.2026
    P = AbsencePeriod
    p = Scenario(report_date=start, current=P(start=start, end=start), previous=[
        P(start=dt.date(2026, 3, 1), end=dt.date(2026, 3, 10)),                    # before the 12 months
        P(start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 5)),                     # gap to next 177 days
        P(start=dt.date(2027, 1, 10), end=dt.date(2027, 1, 12), certified=False),  # no sick note, after certified
        P(start=dt.date(2027, 3, 1), end=dt.date(2027, 3, 20))])                   # gap to current 72 days
    v = facts.evaluate(p)
    assert (v["certified_prior_count"], v["prior_ends_before_12m"], v["uncertified_inside"]) == (3, True, True)
    assert v["latest_gap_within_6m"] and not v["chain_link_exactly_6m"]
    assert v["prior_gaps_under_180d"]  # gaps 174, 176 and 72 days; the absence without a sick note is skipped
    assert z3_facts(p) == v
    # move the oldest illness 7 days earlier: its gap becomes 181 days and the chain breaks
    longer = p.model_copy(update={"previous": [P(start=dt.date(2026, 2, 22), end=dt.date(2026, 3, 3)), *p.previous[1:]]})
    assert not facts.evaluate(longer)["prior_gaps_under_180d"] and z3_facts(longer) == facts.evaluate(longer)



def test_runs_mean_exactly_the_same_as_the_pairs():
    from specpilot.tools.z3_model import exact_link_pairs, exact_link_runs
    for start in (dt.date(2027, 3, 14), dt.date(2027, 2, 28), dt.date(2027, 8, 31), dt.date(2027, 12, 31)):
        pairs = list(exact_link_pairs(start))
        unpacked = [(e, e + dist) for lo, hi, dist in exact_link_runs(start) for e in range(lo, hi + 1)]
        assert unpacked == pairs
        assert len(exact_link_runs(start)) < len(pairs) / 5
