"""The decision table for reason 41: one positive case, one case per condition flipped, then boundaries."""
from datetime import date

import pytest

from specpilot.models import AbsencePeriod, CaseKind, Scenario, TestCase
from specpilot.tools.reference_model import P135, P136, expected


def P(a, b, certified=True):
    return AbsencePeriod(start=date.fromisoformat(a), end=date.fromisoformat(b), certified=certified)


CURRENT = P("2026-10-01", "2026-10-17")  # 17 days
PREVIOUS = P("2026-06-01", "2026-06-20")  # 20 days -> 37 in total


def case(current=CURRENT, previous=(PREVIOUS,), report_date=date(2026, 10, 20), **scenario):
    return TestCase(
        id="TC-3.12-001",
        requirement_ids=["R-3.12-001"],
        kind=CaseKind.POSITIVE,
        title="t",
        check="c",
        scenario=Scenario(report_date=report_date, current=current, previous=list(previous), **scenario),
    )


@pytest.mark.parametrize(
    "tc, send",
    [
        (case(), True),  # TC-01 all conditions met
        (case(current=P("2026-10-01", "2026-10-17", certified=False)), False),  # TC-02 no sick note
        (case(previous=[P("2026-02-01", "2026-03-20")]), False),  # TC-03 nothing in the 6 months before 01.10.
        (case(previous=[P("2026-06-01", "2026-06-12")]), False),  # TC-04 17 + 12 = 29 days
        (case(statutory_insured=False), False),  # TC-05 private insurance
        (case(marginal_employment=True), False),  # Minijob
        (case(previous=[P("2026-06-01", "2026-06-13")]), True),  # TC-06 exactly 30 days
    ],
)
def test_decision_table(tc, send):
    assert expected(tc).message_expected is send


def test_positive_case_fields_and_reasons():
    e = expected(case())
    assert e.fields == {"ABGABEGRUND": "41", "AU-AB-AG": "20261001", "ANZAHL-AU": "01"}
    assert e.periods == [PREVIOUS]
    assert any(r.startswith("C3 ✔ 37 days") for r in e.reasons)


def test_failed_check_is_explained():
    e = expected(case(previous=[P("2026-06-01", "2026-06-12")]))
    assert e.reasons[-1] == "C3 ✘ 29 days < 30 (p.135)"
    assert e.fields == {} and e.periods == []


def test_periods_newest_first_and_chain_stops_at_long_gap():
    previous = [
        P("2025-01-05", "2025-01-20"),  # free time to 01.03.2026 is over 6 months: chain stops before this
        P("2026-03-01", "2026-03-10"),
        P("2026-06-01", "2026-06-20"),
    ]
    e = expected(case(previous=previous))
    assert e.periods == [P("2026-06-01", "2026-06-20"), P("2026-03-01", "2026-03-10")]
    assert e.fields["ANZAHL-AU"] == "02"


def test_uncertified_short_absence_is_not_reported():
    e = expected(case(previous=[PREVIOUS, P("2026-08-03", "2026-08-05", certified=False)]))
    assert e.periods == [PREVIOUS]


def test_open_end_counts_to_report_date_plus_7():
    # current 15.10. - open; report 20.10. -> counted to 27.10. = 13 days; + 17 previous = 30
    e = expected(case(current=P("2026-10-15", "2026-10-15"), previous=[P("2026-06-01", "2026-06-17")], open_end=True))
    assert e.message_expected and "open end" in e.reasons[-2]


def test_the_two_readings_differ_only_where_the_spec_does():
    # free time between the two previous illnesses (01.04. - 30.09.) is exactly 6 months
    exactly_6 = [P("2026-03-20", "2026-03-31"), P("2026-10-01", "2026-10-20")]
    current = P("2026-11-10", "2026-11-20")
    tc = case(current=current, previous=exactly_6, report_date=date(2026, 11, 21))
    assert len(expected(tc, P135).periods) == 2
    assert len(expected(tc, P136).periods) == 1

    # chain going back more than 12 months: p.135 has no limit, p.136 stops at 12 months
    long_chain = [P("2025-06-01", "2025-06-30"), P("2025-11-01", "2025-11-30"), P("2026-04-01", "2026-04-30")]
    assert len(expected(case(previous=long_chain), P135).periods) == 3
    assert len(expected(case(previous=long_chain), P136).periods) == 2
