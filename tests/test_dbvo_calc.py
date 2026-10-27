"""DBVO calculator: hand-made people for every content rule, both readings, plus the content-edge dummy file."""
import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from specpilot import content_review, dbvo
from specpilot.dbvo import ContentResult
from specpilot.models import AbsencePeriod as P, Scenario
from specpilot.review import approve
from specpilot.tools import facts
from specpilot.tools.dbvo_calc import ContentError, compute_content, content_for, must_send
from tests.test_review import RULES, draft

D = dt.date
CURRENT = P(start=D(2027, 7, 1), end=D(2027, 7, 14))  # 6 months before: from 01.01.2027; 12 months: from 01.07.2026


@pytest.fixture(scope="module")
def content():
    return content_review.approve(dbvo.content_card(dbvo.load()), "Examiner A", accept_placeholders=True)


@pytest.fixture(scope="module")
def precondition():
    return approve(draft(), "Examiner A", RULES)


def person(*previous, current=CURRENT) -> Scenario:
    return Scenario(report_date=current.start, current=current, previous=list(previous))


def both(content, s, send=True) -> dict[str, ContentResult]:
    return {r: content_for(content, r, s, send, "REQ-41-X-B01-P1", "DUMMY-0001") for r in content.readings}


def spans(result) -> list[tuple[dt.date, dt.date]]:
    return [(p.start, p.end) for p in result.periods]


# --- the chain, by hand ---------------------------------------------------------------------------------------

def test_short_gaps_report_every_illness_newest_first(content):
    a, b, c = P(start=D(2026, 9, 1), end=D(2026, 9, 10)), P(start=D(2027, 1, 5), end=D(2027, 1, 20)), \
        P(start=D(2027, 4, 1), end=D(2027, 4, 9))
    for r in both(content, person(a, b, c)).values():
        assert spans(r) == [(c.start, c.end), (b.start, b.end), (a.start, a.end)]
        assert r.fields["ANZAHL-AU"] == "03" and r.fields["BEGINN-AU-1"] == "20270401" and r.fields["ENDE-AU-3"] == "20260910"
        assert r.stop_reason == "no further earlier illness"


def test_fields(content):
    r = both(content, person(P(start=D(2027, 4, 1), end=D(2027, 4, 9))))["p.135"]
    assert r.fields == {"ABGABEGRUND": "41", "AU-AB-AG": "20270701", "ANZAHL-AU": "01",
                        "BEGINN-AU-1": "20270401", "ENDE-AU-1": "20270409"}


def test_a_gap_over_6_months_stops_the_chain_in_both_readings(content):
    old, new = P(start=D(2026, 9, 1), end=D(2026, 9, 10)), P(start=D(2027, 4, 1), end=D(2027, 4, 9))  # 202 free days
    for r in both(content, person(old, new)).values():
        assert spans(r) == [(new.start, new.end)] and "more than 6 months" in r.stop_reason


def test_exactly_6_months_continues_under_p135_only(content):
    new = P(start=D(2027, 4, 1), end=D(2027, 4, 9))
    old = P(start=D(2026, 9, 20), end=D(2026, 9, 30))  # free 01.10.2026 - 31.03.2027 = exactly 6 months
    s = person(old, new)
    assert facts.evaluate(s)["chain_link_exactly_6m"]
    r = both(content, s)
    assert r["p.135"].fields["ANZAHL-AU"] == "02" and "exactly 6 months, which continues" in r["p.135"].periods[1].why
    assert r["p.136"].fields["ANZAHL-AU"] == "01" and "exactly 6 months, which ends the chain under p.136" in r["p.136"].stop_reason


def test_one_day_less_than_exactly_continues_in_both(content):
    new = P(start=D(2027, 4, 1), end=D(2027, 4, 9))
    old = P(start=D(2026, 9, 20), end=D(2026, 10, 1))  # free time one day shorter than 6 months
    assert all(r.fields["ANZAHL-AU"] == "02" for r in both(content, person(old, new)).values())


def test_twelve_months_limit_only_under_p136(content):
    chain = [P(start=D(2026, 5, 1), end=D(2026, 5, 20)),   # ends before 01.07.2026: outside the 12 months
             P(start=D(2026, 9, 1), end=D(2026, 9, 15)),
             P(start=D(2027, 2, 1), end=D(2027, 2, 10))]
    r = both(content, person(*chain))
    assert r["p.135"].fields["ANZAHL-AU"] == "03"
    assert r["p.136"].fields["ANZAHL-AU"] == "02" and "12-month limit (01.07.2026)" in r["p.136"].stop_reason


def test_absence_without_a_sick_note_is_skipped_not_a_break(content):
    a = P(start=D(2027, 1, 10), end=D(2027, 1, 20))
    u = P(start=D(2027, 3, 1), end=D(2027, 3, 5), certified=False)
    b = P(start=D(2027, 5, 1), end=D(2027, 5, 10))
    for r in both(content, person(a, u, b)).values():
        assert spans(r) == [(b.start, b.end), (a.start, a.end)]
        assert r.skipped == ["01.03.2027-05.03.2027: no sick note, not a previous illness (certified_only)"]


def test_no_message_no_content(content):
    for r in both(content, person(P(start=D(2027, 4, 1), end=D(2027, 4, 9))), send=False).values():
        assert not r.message_expected and r.fields == {} and r.periods == []
        assert r.explanation().endswith("no message, so no DBVO content")


def test_overlapping_absences_are_refused(content):
    with pytest.raises(ContentError, match="overlap or touch"):
        both(content, person(P(start=D(2027, 3, 1), end=D(2027, 3, 10)), P(start=D(2027, 3, 11), end=D(2027, 3, 20))))


def test_rule_values_come_from_the_card(content):
    """Change a value on the card and the result follows: the logic reads the card, nothing is hard-coded."""
    new = P(start=D(2027, 4, 1), end=D(2027, 4, 9))
    old = P(start=D(2026, 9, 20), end=D(2026, 9, 30))  # exactly 6 months
    edited = content_review.edit_rule(content, "gap_exactly_counts", "p.136", True, "A")
    assert content_for(edited, "p.136", person(old, new), True, "REQ-41-X-B01-P1", "DUMMY-0001").fields["ANZAHL-AU"] == "02"
    oldest = content_review.edit_rule(content, "order", "p.135", "oldest_first", "A")
    r = content_for(oldest, "p.135", person(old, new), True, "REQ-41-X-B01-P1", "DUMMY-0001")
    assert spans(r) == [(old.start, old.end), (new.start, new.end)]


def test_must_send(precondition):
    yes = person(P(start=D(2027, 4, 1), end=D(2027, 4, 30)))  # 30 + 14 days, illness in the last 6 months
    assert must_send(precondition, yes)
    assert not must_send(precondition, yes.model_copy(update={"statutory_insured": False}))


# --- the result form ----------------------------------------------------------------------------------------------

def result_data(content):
    r = both(content, person(P(start=D(2027, 4, 1), end=D(2027, 4, 9)), P(start=D(2027, 5, 1), end=D(2027, 5, 9))))
    return r["p.135"].model_dump()


@pytest.mark.parametrize("change, message", [
    (lambda d: d["fields"].update({"ANZAHL-AU": "3"}), "does not match"),
    (lambda d: d["fields"].update({"BEGINN-AU-1": "20270402"}), "do not match period 1"),
    (lambda d: d["fields"].update({"BEGINN-AU-9": "20270101"}), "beyond the 2 reported"),
    (lambda d: d["fields"].update({"AU-AB-AG": "01.07.2027"}), "must be a date YYYYMMDD"),
    (lambda d: d["periods"][1].update(n=5), "numbered 1, 2"),
    (lambda d: d.update(message_expected=False), "no message, so no content"),
])
def test_inconsistent_results_are_refused(content, change, message):
    data = result_data(content)
    change(data)
    with pytest.raises(ValidationError, match=message):
        ContentResult.model_validate(data)


# --- refusals ---------------------------------------------------------------------------------------------------------

def test_unapproved_content_card_is_refused(precondition, tmp_path):
    with pytest.raises(ContentError, match="not approved"):
        compute_content(dbvo.content_card(dbvo.load()), precondition, tmp_path / "x.sqlite")


def test_unapproved_precondition_card_is_refused(content, tmp_path):
    with pytest.raises(ContentError, match="not approved"):
        compute_content(content, draft(), tmp_path / "x.sqlite")


def test_does_not_use_the_teammates_reference_model():
    assert "reference_model" not in Path("specpilot/tools/dbvo_calc.py").read_text(encoding="utf-8")


# --- on the dummy files ---------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def content_dummy(precondition, tmp_path_factory):
    from specpilot.dummies import build
    from specpilot.tools import scenarios
    from specpilot.tools.branch_maker import make_branches
    from specpilot.tools.dummy_writer import write_outputs
    from specpilot.tools.self_check import check
    from specpilot.tools.value_finder import make_people
    sc = approve(scenarios.build(precondition, RULES), "Examiner A", RULES)
    s = make_branches(sc, background=precondition)
    d = build(make_people(s, 4711), s, "test")
    return write_outputs(d, check(sc, s, d, precondition), tmp_path_factory.mktemp("content")).sqlite


def test_content_people_all_send_and_exact_links_differ_by_reading(content, precondition, content_dummy):
    from specpilot.tools.dummy_writer import load
    res = compute_content(content, precondition, content_dummy)
    d = load(content_dummy)
    assert len(res) == 2 * len(d.people) == 24 and all(r.message_expected for r in res)
    by = {(r.person_id, r.reading): r for r in res}
    for p in d.people:
        if facts.evaluate(d.scenario_of(p))["chain_link_exactly_6m"]:
            assert len(by[(p.person_id, "p.135")].periods) > len(by[(p.person_id, "p.136")].periods), p.person_id


def test_level_1_dummy_file_gives_content_only_to_senders(content, precondition, tmp_path):
    from specpilot.dummies import build
    from specpilot.tools.branch_maker import make_branches
    from specpilot.tools.dummy_writer import write_outputs
    from specpilot.tools.self_check import check
    from specpilot.tools.value_finder import make_people
    s = make_branches(precondition, edges=True)
    d = build(make_people(s, 4711), s, "test")
    path = write_outputs(d, check(precondition, s, d), tmp_path).sqlite
    res = compute_content(content, precondition, path)
    senders = {r.person_id for r in res if r.message_expected}
    assert senders == {p.person_id for p in d.people if p.all_true}


def test_wrong_precondition_card_is_refused(content, precondition, content_dummy):
    from specpilot.criteria import CriteriaCard
    other = approve(CriteriaCard.model_validate(precondition.model_dump() | {
        "version": 2, "status": "draft", "reviewer": "", "reviewed_at": None, "approved_hash": ""}), "A", RULES)
    with pytest.raises(ContentError, match="neither the card nor the background card"):
        compute_content(content, other, content_dummy)
