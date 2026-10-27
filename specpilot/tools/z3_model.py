"""Date model: a dummy person as Z3 unknowns, and every fact as a Z3 formula over them.

The value finder asks Z3 "find a person for whom C1 holds, C3 does not, ..." Z3 can only answer if
each fact is written in its own language. This module does that, once per fact, mirroring the fact
dictionary (specpilot/tools/facts.py); tests/test_z3_model.py proves the two agree on thousands of people.

The trick that keeps it simple: the start of the CURRENT sick period is chosen first (by the dice, in
2027). Then "the 6 months before" and "the 12 months before" are fixed date ranges, and every fact is
plain day arithmetic, which Z3 solves fast.

Dates are day numbers (date.toordinal()). A person has:
    current period  start (fixed) .. end, with sick note yes/no, open end yes/no
    report date     the day the employer sends the request
    0..MAX_PREVIOUS earlier periods  start .. end, sick note yes/no
    insurance       statutory yes/no, Minijob yes/no
Shape rules that keep people realistic and the counting simple:
    every period lasts 1..MAX_LEN days; earlier periods end at least one day before the next one starts
    (no touching or overlapping periods, so no merging is needed) and lie within LOOKBACK_DAYS
    the report date is 0..REPORT_WITHIN days after the current period starts
"""
import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import z3

from specpilot.models import AbsencePeriod, Scenario
from specpilot.tools import facts
from specpilot.tools.date_helpers import gap_vs_months, months_end, window_before
from specpilot.tools.facts import CHAIN_GAP_DAYS, GAP_MONTHS, SUM_MONTHS

MAX_PREVIOUS = 4
MAX_LEN = 60  # days a single sick period may last
REPORT_WITHIN = 40  # report date at most this many days after the current period starts
LOOKBACK_DAYS = 500  # earlier periods start at most this many days before the current one
OPEN_END_DAYS = 7  # an open end counts to the report date + 7 days (p.135, as in facts.py)


@dataclass
class Previous:
    start: z3.ArithRef
    end: z3.ArithRef
    certified: z3.BoolRef


@dataclass
class PersonVars:
    """The unknowns of one dummy person. `start` (current period) is already fixed."""

    start: dt.date
    current_end: z3.ArithRef
    current_certified: z3.BoolRef
    open_end: z3.BoolRef
    report: z3.ArithRef
    statutory: z3.BoolRef
    marginal: z3.BoolRef
    previous: list[Previous]
    ctx: z3.Context | None = None  # the Z3 workspace the unknowns live in

    @property
    def s(self) -> int:
        return self.start.toordinal()


def person_vars(start: dt.date, n_previous: int, name: str = "p", ctx: z3.Context | None = None) -> PersonVars:
    """The unknowns of one person. Pass a fresh `ctx` per puzzle: Z3 keeps internal state between puzzles in
    one workspace, and that state can change which (equally valid) answer it gives. A fresh workspace makes
    the answer depend only on the puzzle, so the same inputs always give the same person."""
    if not 0 <= n_previous <= MAX_PREVIOUS:
        raise ValueError(f"0..{MAX_PREVIOUS} earlier periods, not {n_previous}")
    return PersonVars(
        start=start,
        current_end=z3.Int(f"{name}_current_end", ctx),
        current_certified=z3.Bool(f"{name}_current_certified", ctx),
        open_end=z3.Bool(f"{name}_open_end", ctx),
        report=z3.Int(f"{name}_report", ctx),
        statutory=z3.Bool(f"{name}_statutory", ctx),
        marginal=z3.Bool(f"{name}_marginal", ctx),
        previous=[Previous(z3.Int(f"{name}_prev{i}_start", ctx), z3.Int(f"{name}_prev{i}_end", ctx),
                           z3.Bool(f"{name}_prev{i}_certified", ctx)) for i in range(n_previous)],
        ctx=ctx,
    )


def shape(v: PersonVars) -> list[z3.BoolRef]:
    """Constraints every dummy person must satisfy, whatever the branch."""
    s = v.s
    out = [v.current_end >= s, v.current_end - s + 1 <= MAX_LEN,
           v.report >= s, v.report <= s + REPORT_WITHIN]
    for i, p in enumerate(v.previous):
        out += [p.start <= p.end, p.end - p.start + 1 <= MAX_LEN, p.start >= s - LOOKBACK_DAYS]
        # oldest first; at least one free day before the next period (and before the current one)
        nxt = v.previous[i + 1].start if i + 1 < len(v.previous) else s
        out.append(p.end + 1 < nxt)
    return out


def _max(a, b):
    return z3.If(a > b, a, b)


@lru_cache(maxsize=512)
def gap_end_days(start: dt.date) -> tuple[int, tuple[int, int] | None]:
    """For a current period starting on `start`: the end dates (day numbers) the latest earlier illness may have.
    Returns (first day whose free time is within 6 months, (first, last) day whose free time is EXACTLY 6 months
    or None). Worked out with date_helpers.gap_vs_months, the same rule the fact dictionary uses, so Z3 never
    has to do month arithmetic (6 months are 181-184 days, and month ends are clamped)."""
    within_from, exact = None, []
    for end in range(start.toordinal() - 2, start.toordinal() - LOOKBACK_DAYS - 2, -1):
        v = gap_vs_months(dt.date.fromordinal(end), start, GAP_MONTHS)
        if v > 0:
            break
        within_from = end
        if v == 0:
            exact.append(end)
    return within_from, ((min(exact), max(exact)) if exact else None)


def fact_exprs(v: PersonVars) -> dict[str, z3.ExprRef]:
    """Every fact of the dictionary as a Z3 formula. Mirrors facts.py exactly (see the tests)."""
    s = v.s
    six = window_before(v.start, GAP_MONTHS).start.toordinal()
    twelve = window_before(v.start, SUM_MONTHS).start.toordinal()
    prior_6m = z3.Or([z3.And(p.certified, p.end >= six) for p in v.previous]) if v.previous else z3.BoolVal(False, v.ctx)
    earlier_days = [z3.If(p.certified, _max(p.end - _max(p.start, twelve) + 1, 0), 0) for p in v.previous]
    current_days = z3.If(v.open_end, v.report + OPEN_END_DAYS - s + 1, v.current_end - s + 1)
    exprs = {**chain_exprs(v),
        "current_certified": v.current_certified,
        "prior_certified_6m": prior_6m,
        "total_days_12m": z3.Sum(earlier_days + [current_days]) if earlier_days else current_days,
        "statutory_insured": v.statutory,
        "marginal_employment": v.marginal,
        "current_open_end": v.open_end,
    }
    missing = set(facts.FACTS) - set(exprs)
    if missing:
        raise NotImplementedError(f"no Z3 formula for fact(s) {sorted(missing)}: add one here (and a test)")
    return exprs


@lru_cache(maxsize=512)
def exact_link_pairs(start: dt.date) -> tuple[tuple[int, int], ...]:
    """(end of illness A, start of illness B) day numbers with a free time of EXACTLY 6 months between them,
    for every end day A may have. Free time runs from A.end + 1 to B.start - 1, so B.start - 1 must be the last
    day of the 6 months starting on A.end + 1 (date_helpers.months_end). Worked out here so Z3 never does month
    arithmetic between two unknown dates."""
    out = []
    for end in range(start.toordinal() - LOOKBACK_DAYS - 1, start.toordinal() - 1):
        nxt = months_end(dt.date.fromordinal(end + 1), GAP_MONTHS).toordinal() + 1
        if nxt <= start.toordinal() - 2:
            out.append((end, nxt))
    return tuple(out)


@lru_cache(maxsize=512)
def exact_link_runs(start: dt.date) -> tuple[tuple[int, int, int], ...]:
    """exact_link_pairs packed into runs: (first end day, last end day, distance) where for every end day of A in
    the run, B must start exactly `distance` days later. The distance only changes at month boundaries, so a
    few dozen runs replace hundreds of pairs (same meaning, much smaller puzzle)."""
    runs: list[list[int]] = []
    for end, nxt in exact_link_pairs(start):
        if runs and runs[-1][1] == end - 1 and runs[-1][2] == nxt - end:
            runs[-1][1] = end
        else:
            runs.append([end, end, nxt - end])
    return tuple(tuple(r) for r in runs)


def chain_exprs(v: PersonVars) -> dict[str, z3.ExprRef]:
    """The chain facts (DBVO content) as Z3 formulas. Earlier periods are ordered oldest first and never touch."""
    s, prev, false = v.s, v.previous, z3.BoolVal(False, v.ctx)
    certified = [p.certified for p in prev]
    exists = z3.Or(certified) if prev else false
    latest_end = z3.IntVal(0, v.ctx)
    for p in prev:  # a later certified period overrides an earlier one: the latest certified end
        latest_end = z3.If(p.certified, p.end, latest_end)
    within_from, _ = gap_end_days(v.start)
    runs = exact_link_runs(v.start)
    twelve = window_before(v.start, SUM_MONTHS).start.toordinal()
    pairs, exact_links = [], []
    for i in range(len(prev)):
        for j in range(i + 1, len(prev)):
            consecutive = z3.And([prev[i].certified, prev[j].certified] + [z3.Not(prev[k].certified) for k in range(i + 1, j)])
            pairs.append(z3.Implies(consecutive, prev[j].start - prev[i].end - 1 <= CHAIN_GAP_DAYS))
            exactly = z3.Or([z3.And(prev[i].end >= lo, prev[i].end <= hi, prev[j].start - prev[i].end == dist)
                             for lo, hi, dist in runs])
            exact_links.append(z3.And(consecutive, exactly))
    return {
        "latest_gap_within_6m": z3.And(exists, latest_end >= within_from) if within_from else false,
        "chain_link_exactly_6m": z3.Or(exact_links) if exact_links else false,
        "certified_prior_count": z3.Sum([z3.If(c, 1, 0) for c in certified]) if prev else z3.IntVal(0, v.ctx),
        "prior_ends_before_12m": z3.Or([z3.And(p.certified, p.end < twelve) for p in prev]) if prev else false,
        "uncertified_inside": z3.Or([z3.And(prev[i].certified, z3.Not(prev[j].certified))
                                     for i in range(len(prev)) for j in range(i + 1, len(prev))])
        if len(prev) > 1 else false,
        "prior_gaps_under_180d": z3.And([exists, s - latest_end - 1 <= CHAIN_GAP_DAYS] + pairs) if prev else false,
    }


def condition(expr: z3.ExprRef, op: str, value) -> z3.BoolRef:
    """`expr op value` as a Z3 formula (same operators as facts.holds)."""
    if op == "=":
        return expr == value
    if op == "!=":
        return expr != value
    if op == ">":
        return expr > value
    if op == ">=":
        return expr >= value
    if op == "<":
        return expr < value
    if op == "<=":
        return expr <= value
    if op == "between":
        return z3.And(expr >= value[0], expr <= value[1])
    raise ValueError(f"unknown operator {op!r}")


def to_scenario(v: PersonVars, m: z3.ModelRef) -> Scenario:
    """Read the person Z3 found out of its model."""
    def day(x) -> dt.date:
        return dt.date.fromordinal(m.eval(x, model_completion=True).as_long())

    def yes(x) -> bool:
        return z3.is_true(m.eval(x, model_completion=True))

    return Scenario(
        report_date=day(v.report),
        current=AbsencePeriod(start=v.start, end=day(v.current_end), certified=yes(v.current_certified)),
        previous=[AbsencePeriod(start=day(p.start), end=day(p.end), certified=yes(p.certified)) for p in v.previous],
        statutory_insured=yes(v.statutory),
        marginal_employment=yes(v.marginal),
        open_end=yes(v.open_end),
    )
