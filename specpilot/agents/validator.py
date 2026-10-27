"""Validation agent: compares the vendor's actual output (PDF) with the expected results of our test cases.

Run: python -m specpilot.agents.validator vendor_output.pdf [--out output/validation]

Input
    expected   the testing agent's test cases (table topic_cases): given, steps, expected, cited rules
    vendor     a PDF with the vendor's actual output per test case. Only the case ids (TC-3.7-001) are fixed:
               the text after an id, up to the next id, is the vendor's output for that case. Optional header
               lines before the first case: "Vendor: ...", "Software: ...", "Version: ...".

Per test case in the topics the vendor reported on:
    missing   the id is not in the vendor PDF, or has no text after it
    PASS      the vendor output says what the expected result says (exact text match: code; otherwise GLM judges)
    FAIL      it contradicts the expected result; the judgement names the broken rule(s) of the case
    UNCLEAR   GLM cannot tell from the vendor's text; counted as missing information, never as a failure
GLM only judges. Code fixes the ids, keeps the broken rules to the case's own rules and decides the verdict:
    CERTIFIED            every case was run and passed
    REJECTED             at least one case failed
    MISSING INFORMATION  nothing failed, but cases are missing or unclear, or the PDF names unknown case ids

Output: findings in the database (run id VAL-<time>) and output/validation/<run id>/ with
validation_report.pdf (failures with the spec text behind them, missing information, summary) and report.json.
"""
import argparse
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from specpilot import config, db, llm
from specpilot.models import AuditEntry, Finding, Layer, TopicCase, Verdict
from specpilot.tools import pdf_export

log = logging.getLogger("specpilot.validator")

OUT_DIR = config.ROOT / "output" / "validation"
CASE_ID = re.compile(r"TC-\d+(?:\.\d+)*-\d{3}")
HEADER_KEYS = ("Vendor", "Software", "Version")
CERTIFIED, REJECTED, MISSING_INFORMATION = "CERTIFIED", "REJECTED", "MISSING INFORMATION"

SYSTEM = """You check payroll software against a German specification (DTA EEL).
You get one test case: the situation, the steps, the EXPECTED result, the spec rules behind it (German quote and
English statement) and the ACTUAL output the vendor's software produced.
Decide:
- PASS     the actual output means the same as the expected result (wording, order and format may differ)
- FAIL     the actual output contradicts the expected result or the rules (a wrong value, a missing or extra message, ...)
- UNCLEAR  the actual output does not say enough to decide
For FAIL name the rule id(s) that are broken, the field or point that differs, and the expected and actual value.
Judge only on the given rules; do not add requirements of your own."""


class Judgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["PASS", "FAIL", "UNCLEAR"]
    broken_rule_ids: list[str] = Field(default_factory=list, description="For FAIL: ids of the given rules that are broken")
    point: str = Field(default="", description="For FAIL: the field or point that differs")
    expected_value: str = ""
    actual_value: str = ""
    reason: str = Field(description="One or two sentences why")


class ValidatorError(Exception):
    __test__ = False


# --- reading the vendor PDF ------------------------------------------------------------------------------------

@dataclass
class VendorReport:
    file: str
    sha256: str
    pages: int
    header: dict[str, str]
    outputs: dict[str, str]  # case id -> vendor output text


def read_vendor_pdf(path: Path) -> VendorReport:
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise ValidatorError(f"{path.name} is not a PDF")
    try:
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError) as err:
        raise ValidatorError(f"{path.name} cannot be read: {err}") from err
    if not text.strip():
        raise ValidatorError(f"{path.name} has no text (a scanned PDF needs OCR first)")

    marks = list(CASE_ID.finditer(text))
    head = text[: marks[0].start()] if marks else text
    header = {}
    for key in HEADER_KEYS:
        m = re.search(rf"^\s*{key}\s*:\s*(.+)$", head, re.MULTILINE)
        if m:
            header[key] = m.group(1).strip()
    outputs: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[m.end() : end].strip(" \t\n|:-")
        outputs[m.group()] = f"{outputs[m.group()]}\n{chunk}".strip() if m.group() in outputs else chunk
    return VendorReport(path.name, hashlib.sha256(data).hexdigest(), len(reader.pages), header, outputs)


# --- comparing -------------------------------------------------------------------------------------------------

@dataclass
class RuleRef:
    id: str
    section: str
    section_title: str
    page: int
    quote: str
    statement: str
    document: str
    flags: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    topic: str
    title: str
    given: str
    steps: list[str]
    expected: str
    actual: str
    status: str  # PASS, FAIL, UNCLEAR, MISSING
    reason: str = ""
    point: str = ""
    expected_value: str = ""
    actual_value: str = ""
    broken_rules: list[RuleRef] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


@dataclass
class Validation:
    run_id: str
    vendor: VendorReport
    verdict: str
    cases: list[CaseResult]
    unknown_ids: list[str]
    topics: list[str]
    model: str
    created: str

    def count(self, status: str) -> int:
        return sum(c.status == status for c in self.cases)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _rule_ref(conn: sqlite3.Connection, rule_id: str) -> RuleRef | None:
    r = db.get_requirement(conn, rule_id)
    if r is None:
        return None
    title = next((s["title"] for s in db.list_sections(conn, r.document) if s["number"] == r.section), "")
    return RuleRef(r.id, r.section, title, r.page, r.quote, r.statement, r.document, list(r.flags))


def _prompt(case: TopicCase, rules: list[RuleRef], actual: str) -> str:
    lines = [f"Test case {case.id}: {case.title}", f"Given: {case.given}", "Steps:"]
    lines += [f"  {i}. {s}" for i, s in enumerate(case.steps, start=1)]
    lines += [f"EXPECTED: {case.expected}", "", "Rules:"]
    for r in rules:
        lines += [f"{r.id} (p.{r.page}): {r.statement}", f'    quote: "{r.quote}"']
    lines += ["", f"ACTUAL (vendor output): {actual}"]
    return "\n".join(lines)


def judge(conn: sqlite3.Connection, case: TopicCase, actual: str, model: str = config.LLM_MODEL_CODE) -> CaseResult:
    rules = [r for r in (_rule_ref(conn, i) for i in case.requirement_ids) if r]
    result = CaseResult(case.id, case.section, case.title, case.given, list(case.steps), case.expected, actual, "MISSING",
                        rules=list(case.requirement_ids))
    if not actual.strip():
        result.reason = "the vendor output has no text for this case"
        return result
    if _norm(case.expected) in _norm(actual):
        result.status, result.reason = "PASS", "the vendor output contains the expected result word for word"
        return result

    j = llm.ask_json(model, SYSTEM, _prompt(case, rules, actual), Judgement)
    result.status, result.reason = j.verdict, j.reason
    if j.verdict == "FAIL":
        broken = [r for r in rules if r.id in j.broken_rule_ids] or rules  # never a rule outside the case
        result.broken_rules = broken
        result.point, result.expected_value, result.actual_value = j.point, j.expected_value, j.actual_value
    return result


def decide(cases: list[CaseResult], unknown_ids: list[str]) -> str:
    if any(c.status == "FAIL" for c in cases):
        return REJECTED
    if unknown_ids or any(c.status in ("MISSING", "UNCLEAR") for c in cases):
        return MISSING_INFORMATION
    return CERTIFIED


def validate(conn: sqlite3.Connection, pdf: Path, out_dir: Path = OUT_DIR, model: str = config.LLM_MODEL_CODE) -> Validation:
    vendor = read_vendor_pdf(Path(pdf))
    all_cases = {c.id: c for c in db.list_topic_cases(conn)}
    if not all_cases:
        raise ValidatorError("there are no test cases yet; generate them first")
    if not vendor.outputs:
        raise ValidatorError(f"{vendor.file} names no test case id (like TC-3.7-001)")

    unknown = sorted(i for i in vendor.outputs if i not in all_cases)
    topics = sorted({all_cases[i].section for i in vendor.outputs if i in all_cases}, key=lambda s: [int(n) for n in s.split(".")])
    if not topics:
        raise ValidatorError(f"none of the case ids in {vendor.file} belongs to our test cases")

    first_call = len(llm.calls)
    results = [
        judge(conn, c, vendor.outputs.get(c.id, ""), model)
        for c in sorted(all_cases.values(), key=lambda c: c.id)
        if c.section in topics
    ]
    now = datetime.now()
    v = Validation(f"VAL-{now:%Y%m%d-%H%M%S}", vendor, decide(results, unknown), results, unknown, topics, model,
                   now.isoformat(timespec="seconds"))

    for c in v.cases:
        if c.status == "PASS":
            db.add_finding(conn, v.run_id, Finding(test_case_id=c.id, verdict=Verdict.PASS, layer=Layer.RULES, message=c.reason))
        elif c.status == "FAIL":
            for r in c.broken_rules:
                db.add_finding(conn, v.run_id, Finding(
                    test_case_id=c.id, verdict=Verdict.FAIL, layer=Layer.RULES, requirement_id=r.id,
                    field=c.point or None, expected=c.expected_value or c.expected, actual=c.actual_value or c.actual,
                    message=c.reason,
                ))
    write_report(v, out_dir)
    calls = llm.calls[first_call:]
    db.log(conn, AuditEntry(
        actor="validator", action=f"validated {vendor.file}: {v.verdict}", subject_id=v.run_id,
        model=calls[-1]["model"] if calls else "", cache_hit=bool(calls) and all(c["cache_hit"] for c in calls),
        duration_s=round(sum(c["duration_s"] for c in calls), 2),
        detail=f"{v.count('PASS')} pass, {v.count('FAIL')} fail, {v.count('UNCLEAR')} unclear, {v.count('MISSING')} missing, "
               f"{len(unknown)} unknown ids; sha256 {vendor.sha256}",
    ))
    return v


# --- output ----------------------------------------------------------------------------------------------------

def report_path(run_id: str, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / run_id / "validation_report.pdf"


def write_report(v: Validation, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    folder = out_dir / v.run_id
    folder.mkdir(parents=True, exist_ok=True)
    data = asdict(v)
    data["counts"] = {s: v.count(s) for s in ("PASS", "FAIL", "UNCLEAR", "MISSING")}
    json_path = folder / "report.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_path = report_path(v.run_id, out_dir)
    pdf_export.validation_report_pdf(data, pdf_path)
    return pdf_path, json_path


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description="Validate a vendor output PDF against the expected results.")
    p.add_argument("pdf", type=Path)
    p.add_argument("--out", type=Path, default=OUT_DIR)
    args = p.parse_args()
    v = validate(db.connect(), args.pdf, args.out)
    for c in v.cases:
        print(f"{c.id:<18} {c.status:<8} {c.reason}")
    print(f"\n{v.verdict}: {v.count('PASS')} pass, {v.count('FAIL')} fail, {v.count('UNCLEAR')} unclear, "
          f"{v.count('MISSING')} missing, {len(v.unknown_ids)} unknown ids")
    print(f"report: {report_path(v.run_id, args.out)}")


if __name__ == "__main__":
    main()
