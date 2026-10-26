from datetime import date

import pytest
from pydantic import ValidationError

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

QUOTE_135 = "dem Arbeitgeber für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheinigung vorliegt"


def _rule(**overrides):
    data = dict(
        id="R-3.12-001",
        section="3.12",
        page=135,
        quote=QUOTE_135,
        statement="A reason 41 request may only be sent if the employer has a sick note for the current absence.",
        kind=RuleKind.PRECONDITION,
    )
    return Requirement(**(data | overrides))


def test_sample_requirement():
    r = _rule()
    assert r.status is RuleStatus.DRAFT
    assert r.quote_verified is False
    assert Requirement.model_validate_json(r.model_dump_json()) == r


@pytest.mark.parametrize("bad", [{"id": "3.12-1"}, {"page": 0}, {"quote": "kurz"}, {"invented": "x"}])
def test_requirement_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        _rule(**bad)


def test_period_end_before_start_fails():
    with pytest.raises(ValidationError):
        AbsencePeriod(start=date(2026, 10, 17), end=date(2026, 10, 1))


def test_chain_from_test_case_to_audit():
    tc = TestCase(
        id="TC-3.12-001",
        requirement_ids=["R-3.12-001"],
        kind=CaseKind.NEGATIVE,
        title="No sick note for the current absence",
        check="Is a reason 41 request blocked when the current absence has no sick note?",
        scenario=Scenario(
            report_date=date(2026, 10, 20),
            current=AbsencePeriod(start=date(2026, 10, 1), end=date(2026, 10, 17), certified=False),
            previous=[AbsencePeriod(start=date(2026, 6, 1), end=date(2026, 6, 20))],
        ),
    )
    expected = ExpectedResult(test_case_id=tc.id, message_expected=False)
    finding = Finding(test_case_id=tc.id, verdict=Verdict.PASS, layer=Layer.RULES, requirement_id="R-3.12-001")
    audit = AuditEntry(actor="validator", action="compare", subject_id=tc.id)

    assert not expected.message_expected
    assert finding.verdict.value == "PASS"
    assert audit.at is not None
    assert TestCase.model_validate_json(tc.model_dump_json()) == tc


def test_schema_is_usable_for_ask_json():
    # llm.ask_json sends this schema to the model; it must forbid extra keys
    assert Requirement.model_json_schema()["additionalProperties"] is False
