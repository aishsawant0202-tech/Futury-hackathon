"""SCENARIO data for the verification agent: three vendor reports and the expected results they are checked against.

Everything here is INVENTED for demonstration (rules, card approval, vendor reports) and clearly labelled SCENARIO.
It uses the Testing agent's real code to make the dummy file and the expected results, then writes three vendor
PDF reports (as a vendor would send them) into data/vendor/scenarios/:

    1_certified.pdf       every result matches the expected results          -> CERTIFIED
    2_rejected.pdf        two results differ (a typical off-by-one bug:
                          29 sick days treated as 30, and a wrong end date)   -> REJECTED
    3_missing_information.pdf  the vendor returned only 30 of the 34 test cases  -> MISSING INFORMATION

Run: python -m scripts.make_verification_scenarios      then: python -m specpilot.agents.verification
"""
import datetime as dt
import shutil
import sys
from pathlib import Path

from specpilot import config, content_review, db, dbvo
from specpilot.agents.translator import CardDraft, build_card
from specpilot.models import Actor, Requirement, RuleKind
from specpilot.review import approve
from specpilot.tools import rule_picker
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dbvo_calc import check_content, content_file
from specpilot.tools.dbvo_writer import write_content
from specpilot.tools.dummy_writer import write_outputs
from specpilot.tools.expected_results import check_consistency, compute
from specpilot.tools.expected_writer import write_expected
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from specpilot.dummies import build

SCENARIO_DIR = config.ROOT / "output" / "verification_scenarios"
VENDOR_SCENARIOS = config.ROOT / "data" / "vendor" / "scenarios"
SEED = 4711

# invented rules in the style of § 3.12 (not copied from the spec)
_RULES = [
    ("The current absence must be certified by a sick note.",
     "Die Anfrage setzt voraus, dass für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheinigung vorliegt."),
    ("There must be a certified previous illness in the 6 months before.",
     "Innerhalb der letzten 6 Monate vor Beginn der Arbeitsunfähigkeit muss eine bescheinigte Vorerkrankung liegen."),
    ("Previous illnesses and the current one must cover at least 30 days.",
     "Die Vorerkrankungszeiten müssen zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen."),
    ("Not possible for privately insured employees or Minijobs.",
     "Für privat krankenversicherte und geringfügig beschäftigte Arbeitnehmer ist die Anfrage nicht möglich."),
    ("An open-ended absence is counted up to the report date plus 7 days.",
     "Ist das Ende der Arbeitsunfähigkeit offen, wird bis zum Meldedatum zuzüglich 7 Tage gezählt."),
    ("Go back while the gap is not more than 6 months.",
     "Es ist zurückzugehen, solange zwischen zwei Erkrankungen nicht mehr als 6 Monate liegen."),
    ("Go back at most 12 months.", "Zurückzugehen ist jedoch maximal innerhalb der letzten 12 Monate."),
]

_ANSWER = {
    "logic": "AND",
    "criteria": [
        {"fact": "current_certified", "op": "=", "value": True, "source_rule_ids": ["R-3.12-001"],
         "quote": "eine Arbeitsunfähigkeitsbescheinigung vorliegt"},
        {"fact": "prior_certified_6m", "op": "=", "value": True, "source_rule_ids": ["R-3.12-002"],
         "quote": "Innerhalb der letzten 6 Monate ... bescheinigte Vorerkrankung"},
        {"fact": "total_days_12m", "op": ">=", "value": 30, "source_rule_ids": ["R-3.12-003"],
         "quote": "zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen"},
        {"fact": "statutory_insured", "op": "=", "value": True, "source_rule_ids": ["R-3.12-004"],
         "quote": "Für privat krankenversicherte ... ist die Anfrage nicht möglich"},
        {"fact": "marginal_employment", "op": "=", "value": False, "source_rule_ids": ["R-3.12-004"],
         "quote": "geringfügig beschäftigte Arbeitnehmer ist die Anfrage nicht möglich"},
    ],
    "unused_rules": [{"rule_id": f"R-3.12-00{n}", "reason": "about what is reported, not whether to send"}
                     for n in (5, 6, 7)],
}


# --- a minimal PDF writer (plain text lines, Helvetica) ---------------------------------------------------------

def text_pdf(lines: list[str], path: Path, per_page: int = 55) -> Path:
    """Write ASCII text lines as a simple PDF (no extra library needed)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for page in pages:
        stream = "BT /F1 9 Tf 12 TL 40 800 Td " + " ".join(f"({esc(line)}) Tj T*" for line in page) + " ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode("ascii"))
        content_no = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                       f"/Contents {content_no} 0 R >>".encode("ascii"))
        kids.append(len(objects))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode("ascii")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


# --- the scenario data --------------------------------------------------------------------------------------------

def build_answers(out: Path) -> tuple[Path, dict]:
    """Dummy file + level 1 and level 2 expected results for the invented rules. Returns (dummy file, expected)."""
    conn = db.connect(":memory:")
    for n, (statement, quote) in enumerate(_RULES, start=1):
        db.save_requirement(conn, Requirement(
            id=f"R-3.12-00{n}", section="3.12", page=135, quote=quote, statement=f"[SCENARIO] {statement}",
            kind=RuleKind.PRECONDITION, actor=Actor.EMPLOYER, reason_codes=["41"], quote_verified=True, votes=3))
    rules = db.list_requirements(conn)
    pick = rule_picker.pick(conn, "REQ-41-PRECONDITION")
    card = approve(build_card(pick, CardDraft.model_validate(_ANSWER), "SCENARIO"),
                   "SCENARIO (automatic, not a real review)", rules)
    s = make_branches(card, edges=True)
    d = build(make_people(s, SEED), s, "SCENARIO")
    dummy = write_outputs(d, check(card, s, d), out / "dummies").sqlite
    e = compute(card, dummy)
    write_expected(e, check_consistency(e, d), out / "expected")
    content = content_review.approve(dbvo.content_card(dbvo.load()), "SCENARIO (automatic, not a real review)",
                                     accept_placeholders=True)
    cf = content_file(content, card, dummy)
    write_content(cf, check_content(content, card, dummy, cf), out / "expected")
    expected = {}
    for r in e.results:
        expected[r.neutral_id] = {"person": r.person_id, "message": r.message_expected, "fields": {}}
    for r in cf.results:
        if r.reading == "p.135" and r.message_expected:
            expected[r.neutral_id]["fields"] = dict(r.fields)
    return dummy, expected


def report_lines(vendor: str, version: str, dummy: Path, cases: dict) -> list[str]:
    lines = ["PAYROLL TEST RUN - RESULT REPORT (SCENARIO, invented for demonstration)",
             f"Vendor: {vendor}", "Software: SAP HCM Payroll", f"Version: {version}",
             f"Dummy file: {dummy.stem}", f"Run on: {dt.date(2027, 11, 20):%d.%m.%Y}", ""]
    for neutral in sorted(cases):
        c = cases[neutral]
        parts = [f"Case: {neutral}", f"Meldung: {'ja' if c['message'] else 'nein'}"]
        parts += [f"{k}: {v}" for k, v in c["fields"].items()]
        lines.append(" | ".join(parts))
    return lines


def make(out: Path = SCENARIO_DIR, vendor_dir: Path = VENDOR_SCENARIOS) -> dict[str, Path]:
    if out.exists():
        shutil.rmtree(out)
    for old in vendor_dir.glob("*.pdf") if vendor_dir.exists() else []:
        old.unlink()  # only this scenario folder: earlier scenario reports
    dummy, expected = build_answers(out)
    correct = {n: {"message": c["message"], "fields": dict(c["fields"])} for n, c in expected.items()}

    # 2: the 29-day edge person gets a message (29 treated as 30), and one sender gets a wrong end date
    wrong = {n: {"message": c["message"], "fields": dict(c["fields"])} for n, c in correct.items()}
    at_29 = next(n for n, c in expected.items() if c["person"].endswith("-B05-E1-P1"))
    wrong[at_29] = {"message": True, "fields": {"ABGABEGRUND": "41", "ANZAHL-AU": "01"}}
    sender = next(n for n, c in expected.items() if c["message"] and c["fields"].get("ENDE-AU-1"))
    end = dt.datetime.strptime(wrong[sender]["fields"]["ENDE-AU-1"], "%Y%m%d").date() + dt.timedelta(days=1)
    wrong[sender]["fields"]["ENDE-AU-1"] = f"{end:%Y%m%d}"

    # 3: the vendor ran only part of the test cases
    partial = {n: c for i, (n, c) in enumerate(sorted(correct.items())) if i % 10 != 3}

    vendor_dir.mkdir(parents=True, exist_ok=True)
    return {
        "certified": text_pdf(report_lines("SAP (SCENARIO)", "6.0 EHP8", dummy, correct), vendor_dir / "1_certified.pdf"),
        "rejected": text_pdf(report_lines("SAP (SCENARIO)", "6.0 EHP7", dummy, wrong), vendor_dir / "2_rejected.pdf"),
        "missing_information": text_pdf(report_lines("SAP (SCENARIO)", "6.0 EHP8", dummy, partial),
                                        vendor_dir / "3_missing_information.pdf"),
        "expected": out / "expected",
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    made = make()
    print("SCENARIO data written (invented, for demonstration):")
    for k, v in made.items():
        print(f"  {k:<20} {v}")
    print(f"\nNow run: python -m specpilot.agents.verification --folder {VENDOR_SCENARIOS} --expected {made['expected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
