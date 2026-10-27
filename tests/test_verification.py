"""Verification agent (skeleton): finds and reads the vendor PDF reports."""
from pathlib import Path

from pypdf import PdfWriter

from specpilot.agents import verification
from specpilot.agents.verification import find_reports, read_report


def blank_pdf(path: Path, pages: int = 2) -> Path:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w.write(f)
    return path


def test_vendor_folder_exists_and_reports_are_not_committed():
    assert verification.VENDOR_DIR.name == "vendor" and verification.VENDOR_DIR.parent.name == "data"
    assert (verification.VENDOR_DIR / ".gitkeep").exists()
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "data/vendor/*" in ignore and "!data/vendor/.gitkeep" in ignore


def test_finds_pdfs_also_in_subfolders(tmp_path):
    blank_pdf(tmp_path / "SAP" / "report-b.pdf", pages=3)
    blank_pdf(tmp_path / "report-a.PDF", pages=1)
    (tmp_path / "notes.txt").write_text("not a report", encoding="utf-8")
    reports = find_reports(tmp_path)
    assert [r.path.relative_to(tmp_path).as_posix() for r in reports] == ["report-a.PDF", "SAP/report-b.pdf"]
    assert [r.pages for r in reports] == [1, 3] and all(r.readable for r in reports)
    assert not reports[0].has_text  # blank pages have no text


def test_empty_or_missing_folder(tmp_path):
    assert find_reports(tmp_path) == [] and find_reports(tmp_path / "missing") == []


def test_broken_pdf_is_reported_not_skipped(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"this is not a pdf")
    r = read_report(tmp_path / "broken.pdf")
    assert not r.readable and "cannot be read as a PDF" in r.problem


def test_command_on_an_empty_folder_and_on_unusable_pdfs(tmp_path, capsys):
    assert verification.main(["--folder", str(tmp_path), "--out", str(tmp_path / "res")]) == 0
    assert "No vendor reports" in capsys.readouterr().out
    blank_pdf(tmp_path / "SAP" / "r.pdf")
    (tmp_path / "bad.pdf").write_bytes(b"x")
    assert verification.main(["--folder", str(tmp_path), "--expected", str(tmp_path), "--out", str(tmp_path / "res")]) == 1
    out = capsys.readouterr().out
    assert "VERIFICATION of r.pdf: MISSING INFORMATION" in out and "VERIFICATION of bad.pdf: MISSING INFORMATION" in out
    assert "SUMMARY: 2 MISSING INFORMATION" in out


# --- the three scenarios -----------------------------------------------------------------------------------------

import pytest  # noqa: E402

from specpilot.agents.verification import (CERTIFIED, MISSING_INFORMATION, REJECTED, load_expected,  # noqa: E402
                                           read_vendor_report, verify, write_result)


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    from scripts.make_verification_scenarios import make
    root = tmp_path_factory.mktemp("scenarios")
    return make(root / "out", root / "vendor")


def test_scenario_1_everything_matches_certified(scenarios):
    v = verify(scenarios["certified"], scenarios["expected"])
    assert v.verdict == CERTIFIED and v.cases_compared == v.cases_expected == v.matches == 34
    assert v.differences == [] and v.missing_information == []
    assert len(v.answers_used) == 2  # level 1 and level 2 answers


def test_scenario_2_differences_rejected(scenarios):
    v = verify(scenarios["rejected"], scenarios["expected"])
    assert v.verdict == REJECTED and v.cases_compared == 34 and v.matches == 32
    whats = sorted(d.what.split(" (")[0] for d in v.differences)
    assert whats == ["DBVO field ENDE-AU-1", "message sent or not"]
    assert [d.flag for d in v.differences] == ["E1", "E2"] and v.missing_information == []
    msg = next(d for d in v.differences if d.what == "message sent or not")
    assert (msg.expected, msg.vendor) == ("no message", "message sent")
    # the flag names the broken rule and this person's value
    assert "'total_days_12m >= 30' is NOT met (this person: 29)" in msg.reason and "R-3.12-003" in msg.reason
    end = next(d for d in v.differences if "ENDE-AU-1" in d.what)
    assert end.reason.startswith("period 1 to report (p.135):")


def test_scenario_3_cases_missing_is_missing_information_not_rejected(scenarios):
    v = verify(scenarios["missing_information"], scenarios["expected"])
    assert v.verdict == MISSING_INFORMATION and v.differences == [] and v.cases_compared == 30
    assert [m.flag for m in v.missing_information] == ["M1", "M2", "M3", "M4"]
    assert [m.what.split(":")[0] for m in v.missing_information] == ["DUMMY-0004", "DUMMY-0014", "DUMMY-0024",
                                                                     "DUMMY-0034"]
    assert "not rejected" in v.text()


def test_command_runs_all_three(scenarios, tmp_path, capsys):
    from specpilot.agents import verification
    folder = scenarios["certified"].parent
    code = verification.main(["--folder", str(folder), "--expected", str(scenarios["expected"]),
                              "--out", str(tmp_path / "results")])
    out = capsys.readouterr().out
    assert code == 1 and "SUMMARY: 1 CERTIFIED, 1 REJECTED, 1 MISSING INFORMATION" in out
    assert len(list((tmp_path / "results").glob("*.verification.json"))) == 3


# --- reading the vendor report -----------------------------------------------------------------------------------

def vendor_pdf(tmp_path, lines, name="r.pdf"):
    from scripts.make_verification_scenarios import text_pdf
    return text_pdf(lines, tmp_path / name)


def test_reading_header_and_cases(tmp_path):
    p = vendor_pdf(tmp_path, ["Some title", "Vendor: ACME (SCENARIO)", "Software: Lohn 9", "Version: 1.2",
                              "Dummy file: REQ-41-X-v1-seed1", "Case: DUMMY-0001 | Meldung: nein",
                              "Case: DUMMY-0002 | Meldung: ja | ANZAHL-AU: 01 | BEGINN-AU-1: 20270101"])
    r = read_vendor_report(p)
    assert (r.vendor, r.software, r.version, r.dummy_file) == ("ACME (SCENARIO)", "Lohn 9", "1.2", "REQ-41-X-v1-seed1")
    assert r.cases["DUMMY-0001"].message is False and r.cases["DUMMY-0002"].fields == {"ANZAHL-AU": "01",
                                                                                      "BEGINN-AU-1": "20270101"}
    assert r.problems == []


def test_reading_problems_are_reported(tmp_path):
    p = vendor_pdf(tmp_path, ["Case: DUMMY-0001 | Meldung: vielleicht", "Case: DUMMY-0002 | Meldung: ja",
                              "Case: DUMMY-0002 | Meldung: nein"])
    problems = read_vendor_report(p).problems
    assert any("must be ja or nein" in x for x in problems) and any("reported twice" in x for x in problems)
    assert any("which vendor" in x for x in problems) and any("which dummy file" in x for x in problems)


def test_unreadable_pdf_is_not_verifiable(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    v = verify(tmp_path / "bad.pdf", tmp_path)
    assert v.verdict == MISSING_INFORMATION and "cannot be read as a PDF" in v.missing_information[0].what


def test_unknown_dummy_file_is_not_verifiable(scenarios, tmp_path):
    p = vendor_pdf(tmp_path, ["Vendor: X", "Dummy file: REQ-99-NOPE-v1-seed1", "Case: DUMMY-0001 | Meldung: ja"])
    v = verify(p, scenarios["expected"])
    assert v.verdict == MISSING_INFORMATION
    assert "no expected results for dummy file REQ-99-NOPE" in v.missing_information[0].what


def test_unknown_test_case_is_not_verifiable(scenarios, tmp_path):
    from scripts.make_verification_scenarios import report_lines
    expected, _ = load_expected("REQ-41-PRECONDITION-v1-seed4711", scenarios["expected"])
    cases = {n: {"message": c.message, "fields": c.fields_by_reading.get("p.135", {})} for n, c in expected.items()}
    cases["DUMMY-9999"] = {"message": False, "fields": {}}
    p = vendor_pdf(tmp_path, report_lines("X", "1", Path("REQ-41-PRECONDITION-v1-seed4711.sqlite"), cases))
    v = verify(p, scenarios["expected"])
    assert v.verdict == MISSING_INFORMATION
    assert v.missing_information[0].what == "DUMMY-9999: in the report, but the dummy file has no such test case"


def test_dbvo_content_may_follow_either_reading(scenarios, tmp_path):
    """Where p.135 and p.136 differ, a vendor following p.136 still matches; the note says which reading."""
    from scripts.make_verification_scenarios import report_lines
    expected, _ = load_expected("REQ-41-PRECONDITION-v1-seed4711", scenarios["expected"])
    cases = {n: {"message": c.message, "fields": c.fields_by_reading.get("p.136", {})} for n, c in expected.items()}
    p = vendor_pdf(tmp_path, report_lines("X", "1", Path("REQ-41-PRECONDITION-v1-seed4711.sqlite"), cases))
    v = verify(p, scenarios["expected"])
    assert v.verdict == CERTIFIED


def test_result_files(scenarios, tmp_path):
    import json
    v = verify(scenarios["rejected"], scenarios["expected"])
    j, t = write_result(v, tmp_path)
    assert json.loads(j.read_text(encoding="utf-8"))["verdict"] == REJECTED
    assert "✘ E1 DUMMY-" in t.read_text(encoding="utf-8")


def test_text_pdf_round_trip(tmp_path):
    lines = [f"line {i} (with brackets) and back" + "\\" + "slash" for i in range(120)]
    p = vendor_pdf(tmp_path, lines)
    from pypdf import PdfReader
    reader = PdfReader(p)
    assert len(reader.pages) == 3
    got = [x for page in reader.pages for x in page.extract_text().splitlines()]
    assert got == lines


def test_vendor_following_p136_where_the_readings_differ(tmp_path, monkeypatch):
    """One person whose DBVO content differs by reading: a vendor matching p.136 is certified, and the note says so;
    content matching neither reading is a difference."""
    from specpilot.agents import verification
    from specpilot.agents.verification import ExpectedCase
    p135 = {"ABGABEGRUND": "41", "ANZAHL-AU": "02", "BEGINN-AU-1": "20270101", "ENDE-AU-1": "20270110",
            "BEGINN-AU-2": "20260601", "ENDE-AU-2": "20260610"}
    p136 = {"ABGABEGRUND": "41", "ANZAHL-AU": "01", "BEGINN-AU-1": "20270101", "ENDE-AU-1": "20270110"}
    monkeypatch.setattr(verification, "load_expected", lambda stem, d: (
        {"DUMMY-0001": ExpectedCase("REQ-41-X-B01-P1", True, {"p.135": p135, "p.136": p136})}, ["x.dbvo.sqlite"]))

    def report(fields):
        line = " | ".join(["Case: DUMMY-0001", "Meldung: ja"] + [f"{k}: {v}" for k, v in fields.items()])
        return vendor_pdf(tmp_path, ["Vendor: X", "Dummy file: REQ-41-X-v1-seed1", line], name=f"{len(fields)}.pdf")

    v = verify(report(p136), tmp_path)
    assert v.verdict == CERTIFIED and v.notes == ["where the spec readings differ, the vendor follows p.136"]
    v = verify(report({**p136, "ANZAHL-AU": "03"}), tmp_path)
    assert v.verdict == REJECTED and v.differences[0].what.startswith("DBVO field ANZAHL-AU")


def test_errors_and_missing_cases_together_is_rejected_with_both_flagged(scenarios, tmp_path):
    """20 test cases given, 18 returned and one of those wrong: the proven error rejects, the gaps are flagged too."""
    from scripts.make_verification_scenarios import report_lines
    expected, _ = load_expected("REQ-41-PRECONDITION-v1-seed4711", scenarios["expected"])
    cases = {n: {"message": c.message, "fields": c.fields_by_reading.get("p.135", {})} for n, c in expected.items()}
    gone = sorted(cases)[:2]
    for n in gone:
        del cases[n]
    wrong = next(n for n in sorted(cases) if not cases[n]["message"])
    cases[wrong] = {"message": True, "fields": {}}
    p = vendor_pdf(tmp_path, report_lines("X", "1", Path("REQ-41-PRECONDITION-v1-seed4711.sqlite"), cases))
    v = verify(p, scenarios["expected"])
    assert v.verdict == REJECTED
    assert [(d.flag, d.case) for d in v.differences] == [("E1", wrong)] and "is NOT met" in v.differences[0].reason
    assert [m.what.split(":")[0] for m in v.missing_information] == gone


def test_missing_message_when_it_must_be_sent_says_all_conditions_met(scenarios, tmp_path):
    from scripts.make_verification_scenarios import report_lines
    expected, _ = load_expected("REQ-41-PRECONDITION-v1-seed4711", scenarios["expected"])
    cases = {n: {"message": c.message, "fields": c.fields_by_reading.get("p.135", {})} for n, c in expected.items()}
    sender = next(n for n in sorted(cases) if cases[n]["message"])
    cases[sender] = {"message": False, "fields": {}}
    p = vendor_pdf(tmp_path, report_lines("X", "1", Path("REQ-41-PRECONDITION-v1-seed4711.sqlite"), cases))
    d = verify(p, scenarios["expected"]).differences
    assert len(d) == 1 and (d[0].expected, d[0].vendor) == ("message sent", "no message")
    assert d[0].reason.startswith("all 5 send conditions are met")
