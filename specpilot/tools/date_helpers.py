"""Date arithmetic for sick-leave periods. Plain code, so every expected result rests on exact numbers.

Conventions (all days count inclusively, as on a sick note "01.10. - 17.10." = 17 days):
- A month period is counted like § 188 BGB: starting 16.01., 6 months end on 15.07.
  The spec does not define this; the reference model (step 9) is where it gets applied.
- Overlapping periods are merged before counting, so no day is counted twice.
"""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from specpilot.models import AbsencePeriod

DAY = timedelta(days=1)


def days(p: AbsencePeriod) -> int:
    """Number of days in a period, both ends included."""
    return (p.end - p.start).days + 1


def months_end(start: date, months: int) -> date:
    """Last day of a period of `months` months beginning on `start` (16.01. + 6 months -> 15.07.)."""
    return start + relativedelta(months=months) - DAY


def window_before(day: date, months: int) -> AbsencePeriod:
    """The `months` months right before `day`, e.g. the 6 months before the current absence starts."""
    return AbsencePeriod(start=day - relativedelta(months=months), end=day - DAY)


def clip(p: AbsencePeriod, window: AbsencePeriod) -> AbsencePeriod | None:
    """The part of `p` inside `window`, or None if they do not overlap."""
    start, end = max(p.start, window.start), min(p.end, window.end)
    return AbsencePeriod(start=start, end=end, certified=p.certified) if start <= end else None


def merge(periods: list[AbsencePeriod]) -> list[AbsencePeriod]:
    """Join overlapping or touching periods (17.10. and 18.10. are one stretch)."""
    result: list[AbsencePeriod] = []
    for p in sorted(periods, key=lambda p: p.start):
        if result and p.start <= result[-1].end + DAY:
            last = result[-1]
            result[-1] = AbsencePeriod(start=last.start, end=max(last.end, p.end), certified=last.certified and p.certified)
        else:
            result.append(p)
    return result


def days_in_window(periods: list[AbsencePeriod], window: AbsencePeriod) -> int:
    """Total days of the periods that fall inside the window, each day counted once."""
    parts = [c for p in periods if (c := clip(p, window))]
    return sum(days(p) for p in merge(parts))


def gap_vs_months(earlier_end: date, later_start: date, months: int) -> int:
    """Compare the free time between two absences with a number of months.

    The free time runs from the day after `earlier_end` to the day before `later_start`.
    Returns -1 (shorter), 0 (exactly `months` months) or 1 (longer). The spec words the
    6-month rule two ways ("nicht mehr als" p.135, "nicht mindestens" p.136), which only
    differ at 0 - so the caller decides what "exactly" means.
    """
    free_start, free_end = earlier_end + DAY, later_start - DAY
    limit = months_end(free_start, months)
    return (free_end > limit) - (free_end < limit)


def current_end(current: AbsencePeriod, report_date: date, open_end: bool) -> date:
    """End of the current absence to count with. Open end: one week from the report date (p.135)."""
    return report_date + timedelta(days=7) if open_end else current.end
