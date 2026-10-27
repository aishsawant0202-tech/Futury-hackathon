"""The fact dictionary: the fixed list of things a criterion is allowed to talk about.

This is the backbone of the translator (rule sentences -> criteria card). The model may only
use the fact names below; it can never invent a field. Each fact is defined ONCE here, and the
same definition is used by:
    - the translator   (which names exist, what they mean, which German words point to them)
    - the generator    (which raw inputs to create, which values are realistic)
    - the checks       (computing a fact from a test person's raw data)
and it is computed exactly like the reference model (tests/test_facts.py proves they agree).

Two kinds of fact:
    raw      typed straight into the payroll software (a yes/no on the sick note, the insurance type)
    derived  computed from raw inputs, mostly dates (total sick days, illness in the last 6 months)

Scope: the preconditions of reason 41 (Anforderung Vorerkrankungsmitteilungen), § 3.12.
Add a fact here, with a test, before any criterion may use it.

Run: python -m specpilot.tools.facts
"""
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from specpilot.models import AbsencePeriod, Scenario
from specpilot.tools.date_helpers import clip, current_end, days, days_in_window, gap_vs_months, window_before
from specpilot.tools.reference_model import GAP_MONTHS, SUM_MONTHS


class FactType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"


class FactKind(str, Enum):
    RAW = "raw"  # entered directly
    DERIVED = "derived"  # computed from raw inputs


# Operators a criterion may use, per fact type
OPS: dict[FactType, tuple[str, ...]] = {
    FactType.BOOLEAN: ("=", "!="),
    FactType.INTEGER: ("=", "!=", ">", ">=", "<", "<=", "between"),
}


@dataclass(frozen=True)
class Fact:
    name: str
    type: FactType
    kind: FactKind
    meaning: str  # plain English, shown to the translator and the examiner
    inputs: tuple[str, ...]  # Scenario fields the fact is computed from
    pages: tuple[int, ...]  # spec pages (Verfahrensbeschreibung 13.4) where the fact is defined
    german_terms: tuple[str, ...]  # wording in the spec that points to this fact
    compute: Callable[[Scenario], bool | int]
    unit: str = ""
    plausible: tuple[int, int] | None = None  # realistic range for integers (for the generator)

    @property
    def ops(self) -> tuple[str, ...]:
        return OPS[self.type]


# --- how each derived fact is computed (mirrors reference_model.expected) ---------------

def _certified_before(s: Scenario) -> list[AbsencePeriod]:
    """Earlier illnesses with a sick note that ended before the current absence began."""
    return [p for p in s.previous if p.certified and p.end < s.current.start]


def _prior_certified_6m(s: Scenario) -> bool:
    six = window_before(s.current.start, GAP_MONTHS)
    return any(clip(p, six) for p in _certified_before(s))


def _total_days_12m(s: Scenario) -> int:
    twelve = window_before(s.current.start, SUM_MONTHS)
    current = AbsencePeriod(start=s.current.start, end=current_end(s.current, s.report_date, s.open_end))
    return days_in_window(_certified_before(s), twelve) + days(current)


# --- chain facts (DBVO content): where the earlier illnesses sit relative to the content rules ----------------
# They steer dummy people onto content edges; the content logic itself lives in the DBVO calculator.

CHAIN_GAP_DAYS = 180  # a free time of at most 180 days is shorter than 6 months whatever the months


def _latest_certified(s: Scenario) -> AbsencePeriod | None:
    earlier = _certified_before(s)
    return max(earlier, key=lambda p: p.end) if earlier else None


def _latest_gap_vs_6m(s: Scenario) -> int | None:
    """-1 / 0 / 1: free time after the latest certified earlier illness is shorter than / exactly / longer than
    6 months (date_helpers.gap_vs_months). None if there is no such illness."""
    latest = _latest_certified(s)
    return None if latest is None else gap_vs_months(latest.end, s.current.start, GAP_MONTHS)


def _chain_link_exactly_6m(s: Scenario) -> bool:
    """Two consecutive certified earlier illnesses (absences without a sick note are skipped) with a free time of
    exactly 6 months between them: where p.135 continues the chain and p.136 stops it."""
    chain = sorted(_certified_before(s), key=lambda p: p.end)
    return any(gap_vs_months(a.end, b.start, GAP_MONTHS) == 0 for a, b in zip(chain, chain[1:]))


def _uncertified_inside(s: Scenario) -> bool:
    earlier = [p for p in s.previous if p.end < s.current.start]
    return any(not u.certified and c.certified and u.end > c.end for u in earlier for c in earlier)


def _prior_gaps_under_180d(s: Scenario) -> bool:
    chain = sorted(_certified_before(s), key=lambda p: p.start)
    if not chain:
        return False
    ends = [p.end for p in chain]
    starts = [p.start for p in chain[1:]] + [s.current.start]
    return all((nxt - end).days - 1 <= CHAIN_GAP_DAYS for end, nxt in zip(ends, starts))


_PERIODS = ("previous[].start", "previous[].end", "previous[].certified")

FACTS: dict[str, Fact] = {f.name: f for f in (
    Fact(
        name="current_certified",
        type=FactType.BOOLEAN,
        kind=FactKind.RAW,
        meaning="The current absence is certified by a doctor's sick note (Arbeitsunfähigkeitsbescheinigung).",
        inputs=("current.certified",),
        pages=(135,),
        german_terms=("Arbeitsunfähigkeitsbescheinigung", "AU-Bescheinigung", "aktuelle AU", "ärztlich festgestellt"),
        compute=lambda s: s.current.certified,
    ),
    Fact(
        name="prior_certified_6m",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning=f"At least one certified earlier illness falls (at least partly) in the {GAP_MONTHS} months "
                f"before the current absence begins.",
        inputs=("current.start", *_PERIODS),
        pages=(135,),
        german_terms=("Vorerkrankung", "Vorerkrankungszeiten", f"innerhalb der letzten {GAP_MONTHS} Monate",
                      "vorangegangene Arbeitsunfähigkeit"),
        compute=_prior_certified_6m,
    ),
    Fact(
        name="total_days_12m",
        type=FactType.INTEGER,
        kind=FactKind.DERIVED,
        meaning=f"Days of certified earlier illness in the {SUM_MONTHS} months before the current absence, "
                f"plus the days of the current absence (each day counted once; open end = report date + 7 days).",
        inputs=("current.start", "current.end", "report_date", "open_end", *_PERIODS),
        pages=(135,),
        german_terms=("zusammen mit der aktuellen AU-Zeit", "mindestens 30 Tage", "Tage umfassen",
                      f"letzten {SUM_MONTHS} Monate"),
        compute=_total_days_12m,
        unit="days",
        plausible=(1, 400),
    ),
    Fact(
        name="statutory_insured",
        type=FactType.BOOLEAN,
        kind=FactKind.RAW,
        meaning="The employee has statutory health insurance (not private).",
        inputs=("statutory_insured",),
        pages=(136,),
        german_terms=("gesetzlich krankenversichert", "privat krankenversichert", "Krankenkasse"),
        compute=lambda s: s.statutory_insured,
    ),
    Fact(
        name="marginal_employment",
        type=FactType.BOOLEAN,
        kind=FactKind.RAW,
        meaning="The employee is marginally employed (Minijob).",
        inputs=("marginal_employment",),
        pages=(136,),
        german_terms=("geringfügig beschäftigt", "geringfügige Beschäftigung", "Minijob"),
        compute=lambda s: s.marginal_employment,
    ),
    Fact(
        name="current_open_end",
        type=FactType.BOOLEAN,
        kind=FactKind.RAW,
        meaning="The current absence has no end date yet in the payroll system.",
        inputs=("open_end",),
        pages=(135,),
        german_terms=("offenes Ende", "Ende der Arbeitsunfähigkeit noch nicht bekannt", "voraussichtlich"),
        compute=lambda s: s.open_end,
    ),
    # --- chain facts (DBVO content, Station 6 level 2) ---
    Fact(
        name="latest_gap_within_6m",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning=f"There is a certified earlier illness, and the free time between the latest one and the current "
                f"absence is at most {GAP_MONTHS} months (exactly {GAP_MONTHS} months included).",
        inputs=("current.start", *_PERIODS),
        pages=(135, 136),
        german_terms=("nicht mehr als 6 Monate", "zwischen zwei Erkrankungen", "Zeitraum zwischen"),
        compute=lambda s: (_latest_gap_vs_6m(s) or 0) <= 0 and _latest_certified(s) is not None,
    ),
    Fact(
        name="chain_link_exactly_6m",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning=f"Two consecutive certified earlier illnesses are exactly {GAP_MONTHS} months apart (free time "
                f"between them): p.135 continues the chain past that gap, p.136 stops at it.",
        inputs=("current.start", *_PERIODS),
        pages=(135, 136),
        german_terms=("nicht mehr als 6 Monate", "nicht mindestens 6 Monate", "zwischen zwei Erkrankungen"),
        compute=_chain_link_exactly_6m,
    ),
    Fact(
        name="certified_prior_count",
        type=FactType.INTEGER,
        kind=FactKind.DERIVED,
        meaning="How many earlier illnesses with a sick note ended before the current absence began.",
        inputs=("current.start", *_PERIODS),
        pages=(135,),
        german_terms=("Vorerkrankungszeiten", "Anzahl", "bescheinigte Erkrankung"),
        compute=lambda s: len(_certified_before(s)),
        unit="periods",
        plausible=(0, 10),
    ),
    Fact(
        name="prior_ends_before_12m",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning=f"A certified earlier illness ends before the {SUM_MONTHS} months before the current absence "
                f"(where 'no time limit', p.135, and 'at most {SUM_MONTHS} months', p.136, disagree).",
        inputs=("current.start", *_PERIODS),
        pages=(135, 136),
        german_terms=("keine zeitliche Begrenzung", "maximal innerhalb der letzten 12 Monate", "zurückzugehen"),
        compute=lambda s: any(p.end < window_before(s.current.start, SUM_MONTHS).start for p in _certified_before(s)),
    ),
    Fact(
        name="uncertified_inside",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning="An earlier absence WITHOUT a sick note lies after an earlier illness with one (it must be skipped).",
        inputs=("current.start", *_PERIODS),
        pages=(135,),
        german_terms=("ohne Arbeitsunfähigkeitsbescheinigung", "keine potenzielle Vorerkrankung", "Kurzerkrankung"),
        compute=_uncertified_inside,
    ),
    Fact(
        name="prior_gaps_under_180d",
        type=FactType.BOOLEAN,
        kind=FactKind.DERIVED,
        meaning=f"There is a certified earlier illness, and every free time in the chain (between certified "
                f"illnesses, and up to the current absence) is at most {CHAIN_GAP_DAYS} days: shorter than 6 months "
                f"in any reading, so the chain never breaks.",
        inputs=("current.start", *_PERIODS),
        pages=(135,),
        german_terms=("zurückzugehen", "nicht mehr als 6 Monate", "bis zur erstmalig bescheinigten Erkrankung"),
        compute=_prior_gaps_under_180d,
    ),
)}


def get(name: str) -> Fact:
    """The fact with this name; a clear error listing the allowed names otherwise."""
    try:
        return FACTS[name]
    except KeyError:
        raise KeyError(f"unknown fact {name!r}; allowed: {', '.join(FACTS)}") from None


def evaluate(s: Scenario) -> dict[str, bool | int]:
    """Every fact computed for one test person."""
    return {name: f.compute(s) for name, f in FACTS.items()}


def holds(actual: bool | int, op: str, value) -> bool:
    """Does `actual <op> value` hold? `value` is [lo, hi] (both included) for between."""
    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    if op == "between":
        lo, hi = value
        return lo <= actual <= hi
    raise ValueError(f"unknown operator {op!r}")


def describe() -> str:
    """The dictionary as text: for the translator prompt and for people."""
    lines = []
    for f in FACTS.values():
        unit = f" ({f.unit})" if f.unit else ""
        lines.append(f"- {f.name}: {f.type.value}{unit}, {f.kind.value}. {f.meaning}")
        lines.append(f"    operators: {' '.join(f.ops)} | spec p.{', p.'.join(map(str, f.pages))} | "
                     f"German: {'; '.join(f.german_terms)}")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 and garble ä, ö, ü, §
    print(f"{len(FACTS)} facts (reason 41 preconditions, § 3.12)\n")
    print(describe())


if __name__ == "__main__":
    main()
