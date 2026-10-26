from datetime import date

import pytest

from specpilot import db
from specpilot.models import (
    AbsencePeriod,
    AuditEntry,
    CaseKind,
    ExpectedResult,
    Finding,
    Layer,
    Requirement,
    RuleKind,
    RuleStatus,
    Scenario,
    TestCase,
    Verdict,
)

DOC = "Verfahrensbeschreibung DTA EEL 13.4"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _rule(rule_id="R-3.12-001"):
    return Requirement(
        id=rule_id,
        section="3.12",
        page=135,
        quote="dem Arbeitgeber für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheinigung vorliegt",
        statement="A reason 41 request may only be sent if the employer has a sick note for the current absence.",
        kind=RuleKind.PRECONDITION,
        flags=["p.135 vs p.136: how far back to go"],
    )


def _case(case_id="TC-3.12-001", kind=CaseKind.POSITIVE, rule_ids=("R-3.12-001",)):
    return TestCase(
        id=case_id,
        requirement_ids=list(rule_ids),
        kind=kind,
        title="Sick note present",
        check="Is a reason 41 request created when a sick note exists?",
        scenario=Scenario(
            report_date=date(2026, 10, 20),
            current=AbsencePeriod(start=date(2026, 10, 1), end=date(2026, 10, 17)),
            previous=[AbsencePeriod(start=date(2026, 6, 1), end=date(2026, 6, 20))],
        ),
    )


def test_requirement_round_trip(conn):
    r = _rule()
    db.save_requirement(conn, r)
    assert db.get_requirement(conn, "R-3.12-001") == r
    assert db.get_requirement(conn, "R-9.99-999") is None


def test_examiner_approval(conn):
    db.save_requirement(conn, _rule())
    db.save_requirement(conn, _rule("R-3.12-002"))
    db.set_status(conn, "R-3.12-001", RuleStatus.APPROVED, "matches p.135")

    approved = db.list_requirements(conn, RuleStatus.APPROVED)
    assert [r.id for r in approved] == ["R-3.12-001"]
    assert approved[0].reviewer_note == "matches p.135"
    with pytest.raises(KeyError):
        db.set_status(conn, "R-9.99-999", RuleStatus.APPROVED)


def test_page_search_finds_german_words(conn):
    db.save_page(conn, DOC, 135, "Die Vorerkrankungsanfrage (3.1.18 „Abgabegrund“ = „41“) darf nur ...", "3.12")
    db.save_page(conn, DOC, 136, "Sofern der Arbeitnehmer privat krankenversichert ...", "3.12")

    hits = db.search_pages(conn, "Vorerkrankungsanfrage")
    assert [(h["page"], h["section"]) for h in hits] == [(135, "3.12")]
    assert "[Vorerkrankungsanfrage]" in hits[0]["snippet"]
    assert db.get_page(conn, DOC, 136).startswith("Sofern")


def test_saving_a_page_again_replaces_its_index(conn):
    db.save_page(conn, DOC, 135, "alter Text Arbeitsunfähigkeit")
    db.save_page(conn, DOC, 135, "neuer Text Vorerkrankung")
    assert db.search_pages(conn, "alter") == []
    assert len(db.search_pages(conn, "neuer")) == 1


def test_coverage_shows_gaps(conn):
    for rid in ("R-3.12-001", "R-3.12-002"):
        db.save_requirement(conn, _rule(rid))
        db.set_status(conn, rid, RuleStatus.APPROVED)
    db.save_test_case(conn, _case("TC-3.12-001", CaseKind.POSITIVE))
    db.save_test_case(conn, _case("TC-3.12-002", CaseKind.NEGATIVE))

    assert db.coverage(conn) == [
        {"requirement_id": "R-3.12-001", "positive": 1, "negative": 1, "boundary": 0},
        {"requirement_id": "R-3.12-002", "positive": 0, "negative": 0, "boundary": 0},
    ]
    assert [t.id for t in db.list_test_cases(conn, "R-3.12-001")] == ["TC-3.12-001", "TC-3.12-002"]


def test_test_case_must_point_to_existing_rule(conn):
    with pytest.raises(Exception):
        db.save_test_case(conn, _case(rule_ids=("R-3.12-404",)))
    assert db.get_test_case(conn, "TC-3.12-001") is None  # whole save rolled back


def test_expected_findings_and_audit(conn):
    db.save_requirement(conn, _rule())
    tc = _case()
    db.save_test_case(conn, tc)
    db.save_expected(conn, ExpectedResult(test_case_id=tc.id, message_expected=True, fields={"ABGABEGRUND": "41"}))
    db.add_finding(conn, "run-1", Finding(test_case_id=tc.id, verdict=Verdict.PASS, layer=Layer.FIELDS))
    db.add_finding(conn, "run-2", Finding(test_case_id=tc.id, verdict=Verdict.FAIL, layer=Layer.FIELDS, field="ANZAHL-AU", expected="04", actual="03"))
    db.log(conn, AuditEntry(actor="extractor", action="extract", subject_id="R-3.12-001", model="vllm/Kolibri-1", cache_hit=True, duration_s=0.4))

    assert db.get_expected(conn, tc.id).fields == {"ABGABEGRUND": "41"}
    assert len(db.list_findings(conn)) == 2
    fails = db.list_findings(conn, run_id="run-2", verdict=Verdict.FAIL)
    assert [(f.field, f.actual) for f in fails] == [("ANZAHL-AU", "03")]
    [entry] = db.list_audit(conn, "R-3.12-001")
    assert entry.cache_hit and entry.model == "vllm/Kolibri-1"
