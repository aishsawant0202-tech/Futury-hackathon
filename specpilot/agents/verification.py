"""Verification agent: cross-checks a vendor's report (e.g. from SAP) with the expected results, and certifies or
rejects it.

    vendor PDF report (data/vendor/)      what the vendor's payroll software produced for each dummy test case
    expected results  (output/expected/)  what correct software must produce (the Testing agent's answers):
                                          level 1  <dummy>.expected.sqlite  must the message be sent?
                                          level 2  <dummy>.dbvo.sqlite      the DBVO fields, per spec reading

Per test case (matched by the neutral id DUMMY-0001 ... that the vendor got in the stripped CSV):
    message   did the vendor send the message exactly when it must be sent?
    fields    when sent and level 2 answers exist: are ABGABEGRUND, AU-AB-AG, ANZAHL-AU, BEGINN-AU-n, ENDE-AU-n
              equal to the expected values under at least one reading of the spec (p.135 or p.136)?

Verdicts
    CERTIFIED            every test case was run and every result matches
    REJECTED             at least one result differs. Every error is flagged (E1, E2, ...) with the test case, what
                         was expected, what the vendor reported, and the rule that makes the expected answer right.
                         Test cases missing at the same time are flagged too.
    MISSING INFORMATION  no result differs, but the vendor did not send enough to judge: test cases missing
                         (e.g. 20 given, 18 returned), unknown test cases, unknown dummy file, unreadable PDF.
                         Not rejected: each missing item is flagged (M1, M2, ...) and the vendor must complete it.
The verdict prepares the examiner's decision; the final certification decision stays with the examiner.

Vendor report layout (PROVISIONAL until a real vendor report is available; only read_vendor_report knows it):
    Vendor: SAP
    Software: SAP HCM Payroll
    Version: 6.0 EHP8
    Dummy file: REQ-41-PRECONDITION-v1-seed4711
    Case: DUMMY-0001 | Meldung: nein
    Case: DUMMY-0002 | Meldung: ja | ABGABEGRUND: 41 | AU-AB-AG: 20270713 | ANZAHL-AU: 01 | BEGINN-AU-1: ... | ENDE-AU-1: ...

Run: python -m specpilot.agents.verification [--folder data/vendor] [--expected output/expected] [--out output/verification]
"""
import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from specpilot import config

VENDOR_DIR = config.ROOT / "data" / "vendor"
EXPECTED_DIR = config.ROOT / "output" / "expected"
OUT_DIR = config.ROOT / "output" / "verification"
VERSION = "verification 0.1"

CERTIFIED, REJECTED, MISSING_INFORMATION = "CERTIFIED", "REJECTED", "MISSING INFORMATION"


# --- finding and reading vendor PDFs ---------------------------------------------------------------------------

@dataclass
class VendorReport:
    path: Path
    pages: int = 0
    has_text: bool = False
    problem: str = ""  # why the file could not be read; empty when it was read

    @property
    def readable(self) -> bool:
        return not self.problem


def read_report(path: Path) -> VendorReport:
    """Basic details of one vendor PDF. A damaged or protected file is reported, never silently skipped."""
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            return VendorReport(path, problem="the PDF is password-protected")
        text = "".join((page.extract_text() or "") for page in reader.pages[:3])
        return VendorReport(path, pages=len(reader.pages), has_text=bool(text.strip()))
    except (PdfReadError, OSError, ValueError) as err:
        return VendorReport(path, problem=f"cannot be read as a PDF: {err}")


def find_reports(folder: Path = VENDOR_DIR) -> list[VendorReport]:
    """Every PDF in the vendor folder (and its subfolders, e.g. one per vendor), in name order."""
    folder = Path(folder)
    if not folder.exists():
        return []
    pdfs = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
    # the same order on every system (Windows compares names without regard to capitals, Linux does not)
    return [read_report(p) for p in sorted(pdfs, key=lambda p: p.relative_to(folder).as_posix().lower())]


@dataclass
class VendorCase:
    message: bool
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedReport:
    vendor: str = ""
    software: str = ""
    version: str = ""
    dummy_file: str = ""
    cases: dict[str, VendorCase] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # lines that could not be read, duplicates, ...


_HEADER = re.compile(r"^(Vendor|Software|Version|Dummy file):\s*(.+?)\s*$")
_CASE = re.compile(r"^Case:\s*(DUMMY-\d{4,})\s*\|\s*(.*)$")


def read_vendor_report(path: Path) -> ParsedReport:
    """Read the PROVISIONAL vendor layout out of the PDF text. Replace only this function for a real format."""
    out = ParsedReport()
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            out.problems.append("the PDF is password-protected")
            return out
        lines = [line.strip() for page in reader.pages for line in (page.extract_text() or "").splitlines()]
    except (PdfReadError, OSError, ValueError) as err:
        out.problems.append(f"cannot be read as a PDF: {err}")
        return out
    for line in lines:
        if not line:
            continue
        h = _HEADER.match(line)
        if h:
            setattr(out, {"Vendor": "vendor", "Software": "software", "Version": "version",
                          "Dummy file": "dummy_file"}[h.group(1)], h.group(2))
            continue
        c = _CASE.match(line)
        if not c:
            continue  # titles and free text are allowed
        neutral, parts = c.group(1), [p.strip() for p in c.group(2).split("|")]
        values = dict(p.split(":", 1) for p in parts if ":" in p)
        values = {k.strip(): v.strip() for k, v in values.items()}
        sent = values.pop("Meldung", "").lower()
        if sent not in ("ja", "nein"):
            out.problems.append(f"{neutral}: 'Meldung' must be ja or nein, got {sent!r}")
            continue
        if neutral in out.cases:
            out.problems.append(f"{neutral} is reported twice")
            continue
        out.cases[neutral] = VendorCase(message=sent == "ja", fields=values)
    for name in ("vendor", "dummy_file"):
        if not getattr(out, name):
            out.problems.append(f"the report does not say which {name.replace('_', ' ')} it is")
    return out


# --- the expected results ------------------------------------------------------------------------------------------

@dataclass
class ExpectedCase:
    person_id: str
    message: bool
    fields_by_reading: dict[str, dict[str, str]] = field(default_factory=dict)  # empty without level 2 answers
    conditions: list[dict] = field(default_factory=list)       # level 1: each send condition, met or not, and its rule
    chain_by_reading: dict[str, dict] = field(default_factory=dict)  # level 2: periods (with why) and stop reason


def load_expected(dummy_stem: str, expected_dir: Path = EXPECTED_DIR) -> tuple[dict[str, ExpectedCase], list[str]]:
    """The Testing agent's answers for one dummy file, by neutral id. Returns (cases, which answer files were used)."""
    expected_dir = Path(expected_dir)
    level1 = expected_dir / f"{dummy_stem}.expected.sqlite"
    level2 = expected_dir / f"{dummy_stem}.dbvo.sqlite"
    cases: dict[str, ExpectedCase] = {}
    used = []

    def rows(path, sql):
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    if level1.exists():
        used.append(level1.name)
        for person, neutral, sent in rows(level1, "SELECT person_id, neutral_id, MAX(message_expected) "
                                                  "FROM expected_results GROUP BY person_id, neutral_id"):
            cases[neutral] = ExpectedCase(person, bool(sent))
        by_person = {c.person_id: c for c in cases.values()}
        seen = set()
        for person, crit, fact, op, limit, value, met, rule_ids, quote in rows(level1, (
                "SELECT c.person_id, c.criterion_id, c.fact, c.op, c.limit_json, c.value_json, c.met, c.rule_ids, "
                "c.quote FROM expected_conditions c JOIN expected_results r USING (person_id, reading) "
                "ORDER BY r.position, c.criterion_id")):
            if (person, crit) in seen or person not in by_person:
                continue  # the same condition under a second reading: keep the first
            seen.add((person, crit))
            by_person[person].conditions.append({
                "criterion": crit, "fact": fact, "op": op, "limit": json.loads(limit), "value": json.loads(value),
                "met": bool(met), "rules": ", ".join(json.loads(rule_ids)), "quote": quote})
    if level2.exists():
        used.append(level2.name)
        for person, neutral, reading, sent in rows(level2, "SELECT person_id, neutral_id, reading, message_expected "
                                                           "FROM dbvo_results ORDER BY position"):
            case = cases.setdefault(neutral, ExpectedCase(person, bool(sent)))
            if sent:
                case.fields_by_reading[reading] = {}
        for person, reading, name, value in rows(level2, "SELECT f.person_id, f.reading, f.field, f.value "
                                                         "FROM dbvo_fields f ORDER BY f.person_id, f.reading, f.position"):
            neutral = next(n for n, c in cases.items() if c.person_id == person)
            cases[neutral].fields_by_reading.setdefault(reading, {})[name] = value
        by_person = {c.person_id: c for c in cases.values()}
        for person, reading, periods, stop in rows(level2, "SELECT person_id, reading, periods, stop_reason "
                                                           "FROM dbvo_results"):
            if person in by_person:
                by_person[person].chain_by_reading[reading] = {"periods": periods, "stop": stop, "why": {}}
        for person, reading, n, why in rows(level2, "SELECT person_id, reading, n, why FROM dbvo_periods"):
            if person in by_person and reading in by_person[person].chain_by_reading:
                by_person[person].chain_by_reading[reading]["why"][n] = why
    return cases, used


# --- the cross-check ------------------------------------------------------------------------------------------------

@dataclass
class Difference:
    """One flagged error: a vendor result that does not match the expected result."""
    case: str
    what: str
    expected: str
    vendor: str
    reason: str = ""  # why the expected answer is right (the rule and the person's data)
    flag: str = ""    # E1, E2, ...

    def text(self) -> str:
        lines = [f"{self.flag} {self.case} | {self.what}",
                 f"      expected: {self.expected}", f"      vendor:   {self.vendor}"]
        if self.reason:
            lines.append(f"      why:      {self.reason}")
        return "\n".join(lines)


@dataclass
class MissingItem:
    """One flagged gap: information the vendor did not send, so the report cannot be judged completely."""
    what: str
    flag: str = ""  # M1, M2, ...

    def text(self) -> str:
        return f"{self.flag} {self.what}"


def _show(x) -> str:
    return "yes" if x is True else "no" if x is False else str(x)


def _message_reason(exp: ExpectedCase) -> str:
    """Why the message must (not) be sent: the unmet conditions, or that every condition is met."""
    if not exp.conditions:
        return ""
    failed = [c for c in exp.conditions if not c["met"]]
    if not exp.message and failed:
        return "; ".join(f"condition {c['criterion']} '{c['fact']} {c['op']} {_show(c['limit'])}' is NOT met "
                         f"(this person: {_show(c['value'])}), rule {c['rules']}: \"{c['quote']}\"" for c in failed)
    if exp.message:
        return f"all {len(exp.conditions)} send conditions are met (" + ", ".join(
            f"{c['fact']} = {_show(c['value'])}" for c in exp.conditions) + ")"
    return ""


def _field_reason(exp: ExpectedCase, reading: str, name: str) -> str:
    chain = exp.chain_by_reading.get(reading)
    if not chain:
        return ""
    m = re.fullmatch(r"(?:BEGINN|ENDE)-AU-(\d+)", name)
    if m and int(m.group(1)) in chain["why"]:
        return f"period {m.group(1)} to report ({reading}): {chain['why'][int(m.group(1))]}"
    if name == "ANZAHL-AU":
        return f"{chain['periods']} earlier period(s) to report ({reading}); the look-back stops because: {chain['stop']}"
    return ""


@dataclass
class Verification:
    report: str
    verdict: str
    vendor: str = ""
    software: str = ""
    version: str = ""
    dummy_file: str = ""
    answers_used: list[str] = field(default_factory=list)
    cases_expected: int = 0
    cases_compared: int = 0
    matches: int = 0
    differences: list[Difference] = field(default_factory=list)          # flagged errors E1, E2, ...
    missing_information: list[MissingItem] = field(default_factory=list)  # flagged gaps M1, M2, ...
    missing_cases: list[str] = field(default_factory=list)  # test cases with no usable result in the report
    notes: list[str] = field(default_factory=list)
    made_by: str = VERSION

    def text(self) -> str:
        lines = [f"VERIFICATION of {self.report}: {self.verdict}",
                 f"  vendor {self.vendor or '?'} | software {self.software or '?'} {self.version} | "
                 f"dummy file {self.dummy_file or '?'}",
                 f"  expected results: {', '.join(self.answers_used) or 'none found'}",
                 f"  {self.cases_compared} of {self.cases_expected} test case(s) compared, {self.matches} match, "
                 f"{len(self.differences)} difference(s), {len(self.missing_cases)} case(s) with missing information"]
        if self.differences:
            lines.append(f"  ERRORS ({len(self.differences)}): results that do not match the expected results")
            lines += [f"  ✘ {d.text()}" for d in self.differences]
        if self.missing_information:
            lines.append(f"  MISSING INFORMATION ({len(self.missing_information)}): the vendor must send this"
                         + ("" if self.differences else " - not rejected"))
            lines += [f"  ⚑ {m.text()}" for m in self.missing_information]
        lines += [f"  · {n}" for n in self.notes]
        lines.append("  The final certification decision stays with the examiner.")
        return "\n".join(lines)


def _yes_no(b: bool) -> str:
    return "message sent" if b else "no message"


def verify(path: Path, expected_dir: Path = EXPECTED_DIR) -> Verification:
    """Cross-check one vendor PDF with the expected results and decide CERTIFIED / REJECTED / MISSING INFORMATION."""
    parsed = read_vendor_report(Path(path))
    v = Verification(report=Path(path).name, verdict=MISSING_INFORMATION, vendor=parsed.vendor,
                     software=parsed.software, version=parsed.version, dummy_file=parsed.dummy_file)
    gaps = list(parsed.problems)
    if not parsed.dummy_file:
        return _decide(v, gaps)
    stem = parsed.dummy_file.removesuffix(".sqlite")
    expected, v.answers_used = load_expected(stem, expected_dir)
    v.cases_expected = len(expected)
    if not expected:
        gaps.append(f"no expected results for dummy file {stem} in {Path(expected_dir)}")
        return _decide(v, gaps)

    missing = sorted(set(expected) - set(parsed.cases))
    unknown = sorted(set(parsed.cases) - set(expected))
    gaps += [f"{n}: no result in the report (test case not returned)" for n in missing]
    v.missing_cases = missing
    gaps += [f"{n}: in the report, but the dummy file has no such test case" for n in unknown]
    readings_matched: dict[str, set[str]] = {}
    for neutral in sorted(set(expected) & set(parsed.cases)):
        exp, got = expected[neutral], parsed.cases[neutral]
        v.cases_compared += 1
        if got.message != exp.message:
            v.differences.append(Difference(neutral, "message sent or not", _yes_no(exp.message),
                                            _yes_no(got.message), _message_reason(exp)))
            continue
        if exp.message and exp.fields_by_reading:
            fitting = {r for r, f in exp.fields_by_reading.items() if f == got.fields}
            if not fitting:
                reading, best = min(exp.fields_by_reading.items(),
                                    key=lambda rf: sum(rf[1].get(k) != got.fields.get(k) for k in set(rf[1]) | set(got.fields)))
                for k in sorted(set(best) | set(got.fields)):
                    if best.get(k) != got.fields.get(k):
                        v.differences.append(Difference(neutral, f"DBVO field {k} (closest reading {reading})",
                                                        best.get(k, "(no field)"), got.fields.get(k, "(no field)"),
                                                        _field_reason(exp, reading, k)))
                continue
            readings_matched[neutral] = fitting
        v.matches += 1
    only = {r for rs in readings_matched.values() if len(rs) == 1 for r in rs}
    if len(only) > 1:
        v.notes.append(f"the vendor's DBVO content follows different spec readings for different cases ({', '.join(sorted(only))})")
    elif only:
        v.notes.append(f"where the spec readings differ, the vendor follows {only.pop()}")
    return _decide(v, gaps)


def _decide(v: Verification, gaps: list[str]) -> Verification:
    """Number the flags and decide. A proven error rejects; missing information alone never rejects."""
    for i, d in enumerate(v.differences, start=1):
        d.flag = f"E{i}"
    v.missing_information = [MissingItem(g, f"M{i}") for i, g in enumerate(gaps, start=1)]
    v.verdict = REJECTED if v.differences else (MISSING_INFORMATION if v.missing_information else CERTIFIED)
    return v


def write_result(v: Verification, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / Path(v.report).stem
    json_path, text_path = Path(f"{stem}.verification.json"), Path(f"{stem}.verification.txt")
    json_path.write_text(json.dumps(asdict(v), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text_path.write_text(v.text() + "\n", encoding="utf-8")
    return json_path, text_path


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Cross-check vendor reports with the expected results: certify or reject.")
    p.add_argument("--folder", default=str(VENDOR_DIR), help="folder with the vendor PDF reports")
    p.add_argument("--expected", default=str(EXPECTED_DIR), help="folder with the expected results (Testing agent)")
    p.add_argument("--out", default=str(OUT_DIR), help="folder for the verification results")
    a = p.parse_args(argv)
    folder = Path(a.folder)
    reports = find_reports(folder)
    if not reports:
        print(f"No vendor reports in {folder}. Put the vendors' PDF reports (e.g. from SAP) there.")
        return 0
    verdicts = []
    for r in reports:
        v = verify(r.path, Path(a.expected))
        json_path, _ = write_result(v, Path(a.out))
        print(v.text())
        print(f"  -> {json_path}\n")
        verdicts.append(v.verdict)
    print("SUMMARY: " + ", ".join(f"{n} {k}" for k in (CERTIFIED, REJECTED, MISSING_INFORMATION)
                                  if (n := verdicts.count(k))))
    return 0 if all(x == CERTIFIED for x in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
