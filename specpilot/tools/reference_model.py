"""Reference model for reason 41: what correct payroll software must output for a test employee.

Plain code, never a model, so every expected result is exact and repeatable.
Each check cites the spec page it implements (Verfahrensbeschreibung 13.4, § 3.12).

The spec contradicts itself in two places (p.135 vs p.136). Instead of picking one silently,
the caller passes a Reading, and the testing agent builds cases for both:
- gap of exactly 6 months: "nicht mehr als 6 Monate" (p.135) includes it, "nicht mindestens" (p.136) does not
- how far back: "keine zeitliche Begrenzung" (p.135) or "maximal innerhalb der letzten 12 Monate" (p.136)
"""
from dataclasses import dataclass

from specpilot.models import AbsencePeriod, ExpectedResult, TestCase
from specpilot.tools.date_helpers import (
    clip,
    current_end,
    days,
    days_in_window,
    gap_vs_months,
    merge,
    window_before,
)

MIN_DAYS = 30  # p.135
GAP_MONTHS = 6  # p.135, p.136
SUM_MONTHS = 12  # p.135


@dataclass(frozen=True)
class Reading:
    name: str
    exactly_6_months_counts: bool
    lookback_months: int | None  # None = no limit


P135 = Reading("p.135: gap <= 6 months, no time limit", exactly_6_months_counts=True, lookback_months=None)
P136 = Reading("p.136: gap < 6 months, at most 12 months back", exactly_6_months_counts=False, lookback_months=12)


def _fmt(d) -> str:
    return d.strftime("%Y%m%d")  # jjjjmmtt, as in the DBVO fields


def periods_to_report(current: AbsencePeriod, previous: list[AbsencePeriod], reading: Reading) -> list[AbsencePeriod]:
    """Previous illnesses for the DBVO, newest first (p.140).

    Go back from the current absence while the free time between two illnesses stays within
    6 months (p.135); stop at the first longer gap. Only certified periods count: a short absence
    without a sick note is not a potential previous illness (p.135).
    """
    earlier = merge([p for p in previous if p.certified and p.end < current.start])
    limit = window_before(current.start, reading.lookback_months).start if reading.lookback_months else None
    chain, anchor = [], current.start
    for p in sorted(earlier, key=lambda p: p.end, reverse=True):
        gap = gap_vs_months(p.end, anchor, GAP_MONTHS)
        if gap > 0 or (gap == 0 and not reading.exactly_6_months_counts):
            break
        if limit and p.end < limit:
            break
        chain.append(p)
        anchor = p.start
    return chain


def expected(tc: TestCase, reading: Reading = P135) -> ExpectedResult:
    s = tc.scenario
    reasons = []

    def result(send: bool, **extra) -> ExpectedResult:
        return ExpectedResult(test_case_id=tc.id, message_expected=send, reasons=reasons, reading=reading.name, **extra)

    # C4: not for private health insurance or Minijob (p.136)
    if not s.statutory_insured or s.marginal_employment:
        reasons.append("C4 ✘ privately insured or marginal employment: request not possible (p.136)")
        return result(False)
    reasons.append("C4 ✔ statutory insured, not marginal employment (p.136)")

    # C1: sick note for the current absence (p.135)
    if not s.current.certified:
        reasons.append("C1 ✘ no sick note for the current absence (p.135)")
        return result(False)
    reasons.append("C1 ✔ sick note for the current absence (p.135)")

    certified = [p for p in s.previous if p.certified and p.end < s.current.start]

    # C2: at least one certified previous illness in the 6 months before the current absence (p.135)
    six = window_before(s.current.start, GAP_MONTHS)
    if not any(clip(p, six) for p in certified):
        reasons.append(f"C2 ✘ no certified previous illness between {six.start:%d.%m.%Y} and {six.end:%d.%m.%Y} (p.135)")
        return result(False)
    reasons.append(f"C2 ✔ certified previous illness between {six.start:%d.%m.%Y} and {six.end:%d.%m.%Y} (p.135)")

    # C3: previous illnesses in the last 12 months + current absence >= 30 days (p.135)
    twelve = window_before(s.current.start, SUM_MONTHS)
    current = AbsencePeriod(start=s.current.start, end=current_end(s.current, s.report_date, s.open_end))
    total = days_in_window(certified, twelve) + days(current)
    open_note = " (open end: counted to report date + 7 days)" if s.open_end else ""
    if total < MIN_DAYS:
        reasons.append(f"C3 ✘ {total} days < {MIN_DAYS}{open_note} (p.135)")
        return result(False)
    reasons.append(f"C3 ✔ {total} days >= {MIN_DAYS}{open_note} (p.135)")

    chain = periods_to_report(s.current, s.previous, reading)
    reasons.append(f"{len(chain)} previous period(s) reported, newest first (p.135, p.140)")
    fields = {
        "ABGABEGRUND": "41",
        "AU-AB-AG": _fmt(s.current.start),
        "ANZAHL-AU": f"{len(chain):02d}",
    }
    return result(True, fields=fields, periods=chain)
