"""The fact dictionary: its shape, hand-checked values, and agreement with the reference model."""
import random
import re
from datetime import date, timedelta

import pytest

from specpilot.models import AbsencePeriod, CaseKind, Scenario, TestCase
from specpilot.tools import facts
from specpilot.tools.facts import FACTS, FactKind, FactType, evaluate, get, holds
from specpilot.tools.reference_model import P135, P136, expected


def P(a, b, certified=True):
    return AbsencePeriod(start=date.fromisoformat(a), end=date.fromisoformat(b), certified=certified)


CURRENT = P("2026-10-01", "2026-10-17")  # 17 days
PREVIOUS = P("2026-06-01", "2026-06-20")  # 20 days


def scenario(current=CURRENT, previous=(PREVIOUS,), report_date=date(2026, 10, 20), **kw) -> Scenario:
    return Scenario(report_date=report_date, current=current, previous=list(previous), **kw)


# --- shape of the dictionary -------------------------------------------------------------

def test_names_are_unique_snake_case():
    assert len(FACTS) == len({f.name for f in FACTS.values()})
    for name, f in FACTS.items():
        assert name == f.name
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name


def test_every_fact_is_documented():
    for f in FACTS.values():
        assert f.meaning and f.inputs and f.pages and f.german_terms, f.name
        if f.type is FactType.INTEGER:
            assert f.unit and f.plausible and f.plausible[0] < f.plausible[1], f.name
            assert "between" in f.ops and ">=" in f.ops
        else:
            assert f.ops == ("=", "!="), f.name


def test_inputs_are_real_scenario_fields():
    top = set(Scenario.model_fields)
    period = set(AbsencePeriod.model_fields)
    for f in FACTS.values():
        for inp in f.inputs:
            head, _, attr = inp.replace("[]", "").partition(".")
            assert head in top, (f.name, inp)
            if attr:
                assert attr in period, (f.name, inp)


def test_raw_facts_read_one_input_derived_facts_read_several():
    for f in FACTS.values():
        if f.kind is FactKind.RAW:
            assert len(f.inputs) == 1, f.name
        else:
            assert len(f.inputs) > 1, f.name


def test_unknown_fact_lists_the_allowed_names():
    with pytest.raises(KeyError, match="total_days_12m"):
        get("sum_days")
    assert get("total_days_12m").unit == "days"


# --- hand-checked values -------------------------------------------------------------------

def test_base_case():
    v = evaluate(scenario())
    assert v == {"current_certified": True, "prior_certified_6m": True, "total_days_12m": 37,
                 "statutory_insured": True, "marginal_employment": False, "current_open_end": False,
                 # chain facts: one certified illness ending 20.06., 102 free days before 01.10.
                 "latest_gap_within_6m": True, "chain_link_exactly_6m": False, "certified_prior_count": 1,
                 "prior_ends_before_12m": False, "uncertified_inside": False, "prior_gaps_under_180d": True}


def test_types_are_exact():
    for name, value in evaluate(scenario()).items():
        expected_type = bool if FACTS[name].type is FactType.BOOLEAN else int
        assert type(value) is expected_type, name


def test_six_month_window_edge():
    # current starts 01.10.2026 -> the 6 months before are 01.04.2026 - 30.09.2026
    assert not evaluate(scenario(previous=[P("2026-03-01", "2026-03-31")]))["prior_certified_6m"]
    assert evaluate(scenario(previous=[P("2026-03-01", "2026-04-01")]))["prior_certified_6m"]


def test_total_days_29_30_31():
    for end, total in (("2026-06-12", 29), ("2026-06-13", 30), ("2026-06-14", 31)):
        assert evaluate(scenario(previous=[P("2026-06-01", end)]))["total_days_12m"] == total


def test_open_end_counts_to_report_date_plus_7():
    # current 01.10. - (20.10. + 7 = 27.10.) = 27 days, + 20 previous
    assert evaluate(scenario(open_end=True))["total_days_12m"] == 47


def test_overlapping_previous_periods_count_once():
    v = evaluate(scenario(previous=[P("2026-06-01", "2026-06-20"), P("2026-06-10", "2026-06-25")]))
    assert v["total_days_12m"] == 25 + 17


def test_uncertified_and_later_periods_are_ignored():
    v = evaluate(scenario(previous=[P("2026-06-01", "2026-06-20", certified=False), P("2026-09-25", "2026-10-03")]))
    assert v["total_days_12m"] == 17
    assert not v["prior_certified_6m"]


def test_twelve_month_window_clips_a_period():
    # 12 months before 01.10.2026 start on 01.10.2025: only 01.10. - 10.10.2025 counts
    v = evaluate(scenario(previous=[P("2025-09-20", "2025-10-10")]))
    assert v["total_days_12m"] == 10 + 17


# --- operators -----------------------------------------------------------------------------

@pytest.mark.parametrize("actual, op, value, result", [
    (30, ">=", 30, True), (29, ">=", 30, False), (30, ">", 30, False), (31, ">", 30, True),
    (30, "<=", 30, True), (31, "<=", 30, False), (29, "<", 30, True), (30, "<", 30, False),
    (30, "=", 30, True), (30, "!=", 30, False), (True, "=", True, True), (False, "!=", True, True),
    (5, "between", [5, 9], True), (9, "between", [5, 9], True), (10, "between", [5, 9], False),
])
def test_holds(actual, op, value, result):
    assert holds(actual, op, value) is result


def test_holds_rejects_unknown_operator():
    with pytest.raises(ValueError):
        holds(1, "~", 1)


# --- agreement with the reference model ------------------------------------------------------

def preconditions_met(s: Scenario) -> bool:
    """Reason 41 preconditions written with the facts only."""
    v = evaluate(s)
    return (holds(v["current_certified"], "=", True)
            and holds(v["prior_certified_6m"], "=", True)
            and holds(v["total_days_12m"], ">=", 30)
            and holds(v["statutory_insured"], "=", True)
            and holds(v["marginal_employment"], "=", False))


def as_case(s: Scenario) -> TestCase:
    return TestCase(id="TC-3.12-001", requirement_ids=["R-3.12-001"], kind=CaseKind.POSITIVE,
                    title="t", check="c", scenario=s)


def random_scenario(rng: random.Random) -> Scenario:
    start = date(2026, 1, 1) + timedelta(days=rng.randint(0, 365))

    def period(begin: date, max_len: int, p_cert: float) -> AbsencePeriod:
        return AbsencePeriod(start=begin, end=begin + timedelta(days=rng.randint(0, max_len)),
                             certified=rng.random() < p_cert)

    current = period(start, 40, 0.85)
    previous = [period(start - timedelta(days=rng.randint(1, 450)), 40, 0.8) for _ in range(rng.randint(0, 4))]
    return Scenario(report_date=start + timedelta(days=rng.randint(0, 40)), current=current, previous=previous,
                    statutory_insured=rng.random() < 0.85, marginal_employment=rng.random() < 0.1,
                    open_end=rng.random() < 0.2)


@pytest.mark.parametrize("reading", [P135, P136])
def test_facts_agree_with_reference_model_on_random_people(reading):
    rng = random.Random(41)
    outcomes = {True: 0, False: 0}
    near_30 = 0
    for _ in range(3000):
        s = random_scenario(rng)
        ours = preconditions_met(s)
        assert ours == expected(as_case(s), reading).message_expected, s
        outcomes[ours] += 1
        near_30 += abs(evaluate(s)["total_days_12m"] - 30) <= 1
    # the sample must really exercise both outcomes and the 30-day edge, or the test proves nothing
    assert outcomes[True] > 100 and outcomes[False] > 100, outcomes
    assert near_30 > 20, near_30


def test_every_fact_takes_both_values_or_a_spread_on_random_people():
    rng = random.Random(7)
    seen: dict[str, set] = {name: set() for name in FACTS}
    for _ in range(2000):
        for name, value in evaluate(random_scenario(rng)).items():
            seen[name].add(value)
    for name, values in seen.items():
        if name == "chain_link_exactly_6m":
            continue  # too rare for random people; tests/test_z3_model.py places people exactly on that edge
        if FACTS[name].type is FactType.BOOLEAN:
            assert values == {True, False}, name
        else:
            lo, hi = FACTS[name].plausible
            spread = 30 if FACTS[name].unit == "days" else 4  # a count of 0-4 earlier illnesses has 5 values
            assert len(values) > spread and all(lo <= v <= hi for v in values), (name, min(values), max(values))


def test_describe_lists_every_fact():
    text = facts.describe()
    for name in FACTS:
        assert f"- {name}:" in text
