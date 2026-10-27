from datetime import date

import pytest

from specpilot.models import AbsencePeriod
from specpilot.tools.date_helpers import (
    clip,
    current_end,
    days,
    days_in_window,
    gap_vs_months,
    merge,
    months_end,
    window_before,
)


def P(a, b, certified=True):
    return AbsencePeriod(start=date.fromisoformat(a), end=date.fromisoformat(b), certified=certified)


def test_days_are_inclusive():
    assert days(P("2026-10-01", "2026-10-17")) == 17
    assert days(P("2026-10-01", "2026-10-01")) == 1


@pytest.mark.parametrize("previous_end, total", [("2026-06-12", 29), ("2026-06-13", 30), ("2026-06-14", 31)])
def test_29_30_31_days(previous_end, total):
    # current 01.10.-17.10. (17 days) + previous 01.06.-xx.06. inside the 12 months before 01.10.
    current = P("2026-10-01", "2026-10-17")
    previous = P("2026-06-01", previous_end)
    window = window_before(current.start, 12)
    assert days_in_window([previous], window) + days(current) == total


def test_months_follow_the_calendar():
    assert months_end(date(2026, 1, 16), 6) == date(2026, 7, 15)
    assert months_end(date(2026, 4, 1), 6) == date(2026, 9, 30)
    assert months_end(date(2026, 3, 1), 6) == date(2026, 8, 31)
    assert window_before(date(2026, 10, 1), 6) == P("2026-04-01", "2026-09-30")


@pytest.mark.parametrize(
    "earlier_end, later_start, expected",
    [
        ("2026-03-31", "2026-09-30", -1),  # free 01.04.-29.09.: one day short of 6 months
        ("2026-03-31", "2026-10-01", 0),  # free 01.04.-30.09.: exactly 6 months
        ("2026-03-31", "2026-10-02", 1),  # 6 months + 1 day
        ("2026-01-15", "2026-07-16", 0),  # free 16.01.-15.07.
    ],
)
def test_gap_of_exactly_6_months(earlier_end, later_start, expected):
    assert gap_vs_months(date.fromisoformat(earlier_end), date.fromisoformat(later_start), 6) == expected


def test_overlaps_are_counted_once():
    periods = [P("2026-06-01", "2026-06-10"), P("2026-06-05", "2026-06-15"), P("2026-06-16", "2026-06-20")]
    assert merge(periods) == [P("2026-06-01", "2026-06-20")]
    assert days_in_window(periods, P("2026-01-01", "2026-12-31")) == 20


def test_only_days_inside_the_window_count():
    window = window_before(date(2026, 10, 1), 12)  # 01.10.2025 - 30.09.2026
    assert clip(P("2025-09-20", "2025-10-05"), window) == P("2025-10-01", "2025-10-05")
    assert clip(P("2025-01-01", "2025-02-01"), window) is None
    assert days_in_window([P("2025-09-20", "2025-10-05")], window) == 5


def test_open_end_counts_one_week_ahead():
    current = P("2026-10-01", "2026-10-01")
    assert current_end(current, date(2026, 10, 20), open_end=True) == date(2026, 10, 27)
    assert current_end(P("2026-10-01", "2026-10-17"), date(2026, 10, 20), open_end=False) == date(2026, 10, 17)
