"""Validation agent tests with a fake model and invented vendor PDFs: no gateway, no key."""
import json

import pytest
from fpdf import FPDF

from specpilot import db, llm
from specpilot.agents import validator
from specpilot.agents.validator import Judgement, ValidatorError
from specpilot.models import Actor, CaseKind, Requirement, RuleKind, TopicCase, Verdict

DOC = "Test-Spezifikation"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.save_sections(c, DOC, [{"number": "3.7", "title": "DBEE", "first_page": 91, "last_page": 94}])
    for n in (1, 2):
        db.save_requirement(c, Requirement(
            id=f"R-3.7-00{n}", section="3.7", page=91 + n, quote=f"Das Feld muss den Wert {n} enthalten", statement=f"field holds {n}",
            kind=RuleKind.CONTENT, actor=Actor.EMPLOYER, quote_verified=True, document=DOC,
        ))
    db.save_topic_cases(c, "3.7", [
        TopicCase(id=f"TC-3.7-00{n}", section="3.7", requirement_ids=["R-3.7-001", "R-3.7-002"], kind=CaseKind.POSITIVE,
                  title=f"case {n}", given="an employee", steps=["send"], expected=f"Message sent with value {n}")
        for n in (1, 2, 3)
    ])
    yield c
    c.close()


def _vendor_pdf(path, lines):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=10)
    for line in lines:
        pdf.multi_cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def test_read_vendor_pdf(tmp_path):
    path = _vendor_pdf(tmp_path / "v.pdf", ["Vendor: SAP", "Version: 6.0", "TC-3.7-001 | sent with value 1", "TC-3.7-002", "no message"])
    rep = validator.read_vendor_pdf(path)
    assert rep.header == {"Vendor": "SAP", "Version": "6.0"}
    assert rep.outputs == {"TC-3.7-001": "sent with value 1", "TC-3.7-002": "no message"}


def test_non_pdf_and_pdf_without_ids_are_refused(conn, tmp_path):
    txt = tmp_path / "v.pdf"
    txt.write_text("TC-3.7-001 ok")
    with pytest.raises(ValidatorError, match="not a PDF"):
        validator.validate(conn, txt, tmp_path / "out")
    with pytest.raises(ValidatorError, match="names no test case id"):
        validator.validate(conn, _vendor_pdf(tmp_path / "x.pdf", ["just text"]), tmp_path / "out")


def test_rejected_with_spec_text_missing_and_report(conn, tmp_path, monkeypatch):
    def fake(model, system, user, schema):
        assert schema is Judgement and 'quote: "Das Feld muss den Wert 1 enthalten"' in user
        return Judgement(verdict="FAIL", broken_rule_ids=["R-3.7-002", "R-9.9-999"], point="value", expected_value="2",
                         actual_value="7", reason="wrong value")

    monkeypatch.setattr(llm, "ask_json", fake)
    path = _vendor_pdf(tmp_path / "v.pdf", [
        "Vendor: SAP", "TC-3.7-001 | Message sent with value 1", "TC-3.7-002 | Message sent with value 7", "TC-9.9-001 | extra",
    ])
    v = validator.validate(conn, path, tmp_path / "out")

    assert v.verdict == validator.REJECTED
    assert [(c.id, c.status) for c in v.cases] == [("TC-3.7-001", "PASS"), ("TC-3.7-002", "FAIL"), ("TC-3.7-003", "MISSING")]
    assert [r.id for r in v.cases[1].broken_rules] == ["R-3.7-002"]  # the invented rule id is dropped
    assert v.cases[1].broken_rules[0].section_title == "DBEE" and v.cases[1].broken_rules[0].page == 93
    assert v.unknown_ids == ["TC-9.9-001"]

    findings = db.list_findings(conn, v.run_id)
    assert [(f.test_case_id, f.verdict, f.requirement_id) for f in findings] == [
        ("TC-3.7-001", Verdict.PASS, None), ("TC-3.7-002", Verdict.FAIL, "R-3.7-002"),
    ]
    report = validator.report_path(v.run_id, tmp_path / "out")
    assert report.read_bytes().startswith(b"%PDF")
    data = json.loads((report.parent / "report.json").read_text(encoding="utf-8"))
    assert data["counts"] == {"PASS": 1, "FAIL": 1, "UNCLEAR": 0, "MISSING": 1}


def test_verdicts():
    def c(status):
        return validator.CaseResult("TC-1-001", "1", "", "", [], "", "", status)

    assert validator.decide([c("PASS"), c("PASS")], []) == validator.CERTIFIED
    assert validator.decide([c("PASS"), c("UNCLEAR")], []) == validator.MISSING_INFORMATION
    assert validator.decide([c("PASS")], ["TC-9-001"]) == validator.MISSING_INFORMATION
    assert validator.decide([c("FAIL"), c("MISSING")], []) == validator.REJECTED


def test_old_findings_table_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE test_cases (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL)")
    old.execute("""CREATE TABLE findings (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                   test_case_id TEXT NOT NULL REFERENCES test_cases(id), verdict TEXT NOT NULL, layer TEXT NOT NULL,
                   requirement_id TEXT, data TEXT NOT NULL)""")
    old.commit()
    old.close()
    c = db.connect(path)
    from specpilot.models import Finding, Layer

    db.add_finding(c, "VAL-1", Finding(test_case_id="TC-3.7-001", verdict=Verdict.PASS, layer=Layer.RULES))
    assert len(db.list_findings(c, "VAL-1")) == 1
