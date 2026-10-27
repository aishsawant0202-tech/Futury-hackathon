"""Write the test package as PDF: one file for the vendor (test cases), one for the examiner (expected results).

Uses Arial from Windows when it is there (umlauts, „quotes“); otherwise Helvetica with the text cut to Latin-1.
"""
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

_FONT_DIR = Path("C:/Windows/Fonts")
_LATIN1 = str.maketrans({"„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "→": "->"})


class _Pdf(FPDF):
    def __init__(self, title: str):
        super().__init__()
        self.doc_title = title
        if (_FONT_DIR / "arial.ttf").exists():
            self.add_font("body", "", str(_FONT_DIR / "arial.ttf"))
            self.add_font("body", "B", str(_FONT_DIR / "arialbd.ttf"))
            self.unicode = True
        else:
            self.unicode = False
        self.set_auto_page_break(True, margin=15)

    def clean(self, s: str) -> str:
        s = str(s)
        return s if self.unicode else s.translate(_LATIN1).encode("latin-1", "replace").decode("latin-1")

    def font(self, style: str = "", size: int = 10) -> None:
        self.set_font("body" if self.unicode else "helvetica", style, size)

    def header(self):
        self.font("B", 9)
        self.set_text_color(31, 58, 95)
        self.cell(0, 6, self.clean(f"SpecPilot · {self.doc_title}"), align="L")
        self.cell(0, 6, f"{datetime.now():%d.%m.%Y}", align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.font("", 8)
        self.cell(0, 6, f"{self.page_no()} / {{nb}}", align="C")


def _write(title: str, intro: str, rows: list[dict], heading: tuple[str, ...], fields: list[tuple[str, str]], path: Path | None) -> bytes:
    pdf = _Pdf(title)
    pdf.add_page()
    pdf.font("B", 16)
    pdf.cell(0, 10, pdf.clean(title), new_x="LMARGIN", new_y="NEXT")
    pdf.font("", 10)
    pdf.multi_cell(0, 5, pdf.clean(intro), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    for r in rows:
        pdf.set_fill_color(232, 238, 245)
        pdf.font("B", 11)
        pdf.multi_cell(0, 7, pdf.clean("  ·  ".join(str(r[h]) for h in heading if r.get(h))), fill=True, new_x="LMARGIN", new_y="NEXT")
        for key, name in fields:
            if not r.get(key):
                continue
            pdf.font("B", 9)
            pdf.cell(0, 5, pdf.clean(name), new_x="LMARGIN", new_y="NEXT")
            pdf.font("", 9)
            pdf.multi_cell(0, 4.5, pdf.clean(r[key]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    data = bytes(pdf.output())
    if path:
        path.write_bytes(data)
    return data


def test_cases_pdf(rows: list[dict], path: Path | None = None) -> bytes:
    """Rows from tester.export_rows (vendor part): no expected answers, no rule ids."""
    return _write(
        "Test cases", f"{len(rows)} test cases. Enter each situation in the payroll software and send the output file back.",
        rows, ("id", "topic", "title"), [("given", "Given"), ("steps", "Steps")], path,
    )


def expected_results_pdf(rows: list[dict], path: Path | None = None) -> bytes:
    """Rows from tester.export_rows (expected part): the answer per case and the rules it rests on."""
    return _write(
        "Expected results", f"{len(rows)} test cases. What correct software must output, with the rules and spec pages behind it.",
        rows, ("id", "topic", "kind"), [("expected", "Expected"), ("rules", "Rules")], path,
    )


_VERDICT_COLOURS = {"CERTIFIED": (20, 128, 74), "REJECTED": (179, 38, 30), "MISSING INFORMATION": (166, 106, 0)}


def _heading(pdf: _Pdf, text: str) -> None:
    pdf.ln(3)
    pdf.font("B", 14)
    pdf.set_text_color(31, 58, 95)
    pdf.cell(0, 9, pdf.clean(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def _field(pdf: _Pdf, name: str, value: str, size: int = 9) -> None:
    if not value:
        return
    pdf.font("B", size)
    pdf.cell(0, 5, pdf.clean(name), new_x="LMARGIN", new_y="NEXT")
    pdf.font("", size)
    pdf.multi_cell(0, 4.5, pdf.clean(value), new_x="LMARGIN", new_y="NEXT")


def _bar(pdf: _Pdf, text: str, rgb: tuple[int, int, int]) -> None:
    pdf.set_fill_color(*rgb)
    pdf.font("B", 11)
    pdf.multi_cell(0, 7, pdf.clean(text), fill=True, new_x="LMARGIN", new_y="NEXT")


def _table(pdf: _Pdf, head: list[str], rows: list[list], widths: list[int]) -> None:
    pdf.font("B", 9)
    pdf.set_fill_color(232, 238, 245)
    for h, w in zip(head, widths):
        pdf.cell(w, 6, pdf.clean(h), border=1, fill=True)
    pdf.ln()
    pdf.font("", 9)
    for row in rows:
        for v, w in zip(row, widths):
            pdf.cell(w, 6, pdf.clean(str(v)), border=1)
        pdf.ln()


def validation_report_pdf(report: dict, path: Path | None = None) -> bytes:
    """Full validation report from validator.write_report: verdict, failures with their spec text, missing
    information, passed cases and a summary at the end."""
    cases, vendor, counts = report["cases"], report["vendor"], report["counts"]
    failed = [c for c in cases if c["status"] == "FAIL"]
    missing = [c for c in cases if c["status"] in ("MISSING", "UNCLEAR")]
    passed = [c for c in cases if c["status"] == "PASS"]

    pdf = _Pdf("Validation report")
    pdf.add_page()
    pdf.font("B", 18)
    pdf.cell(0, 11, "Validation report", new_x="LMARGIN", new_y="NEXT")
    pdf.font("", 10)
    meta = [
        ("Run", report["run_id"]), ("Date", report["created"].replace("T", " ")),
        ("Vendor output", f"{vendor['file']} ({vendor['pages']} pages)"), ("sha256", vendor["sha256"]),
        *[(k, v) for k, v in vendor["header"].items()],
        ("Topics", ", ".join(report["topics"])), ("Judged by", f"validation agent ({report['model']})"),
    ]
    for k, v in meta:
        pdf.font("B", 10)
        pdf.cell(32, 5.5, pdf.clean(k))
        pdf.font("", 10)
        pdf.multi_cell(0, 5.5, pdf.clean(v), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_text_color(255, 255, 255)
    _bar(pdf, f"  {report['verdict']}   ·   {counts['PASS']} passed · {counts['FAIL']} failed · "
              f"{counts['UNCLEAR']} unclear · {counts['MISSING']} missing of {len(cases)} test cases",
         _VERDICT_COLOURS.get(report["verdict"], (90, 90, 90)))
    pdf.set_text_color(0, 0, 0)
    pdf.font("", 8)
    pdf.multi_cell(0, 4, pdf.clean(
        "AI-drafted, not verified: the expected results were written by the testing agent and each comparison was judged by "
        "the validation agent. The examiner makes the certification decision."), new_x="LMARGIN", new_y="NEXT")

    _heading(pdf, f"1. Failed test cases ({len(failed)})")
    if not failed:
        pdf.font("", 10)
        pdf.cell(0, 6, "No test case failed.", new_x="LMARGIN", new_y="NEXT")
    for c in failed:
        _bar(pdf, f"{c['id']}  ·  topic {c['topic']}  ·  {c['title']}", (250, 228, 226))
        _field(pdf, "Given", c["given"])
        _field(pdf, "Steps", "\n".join(f"{i}. {s}" for i, s in enumerate(c["steps"], start=1)))
        _field(pdf, "Expected result", c["expected"])
        _field(pdf, "Vendor output", c["actual"])
        if c["point"] or c["expected_value"] or c["actual_value"]:
            _field(pdf, "What differs", f"{c['point']}: expected {c['expected_value'] or '-'}, vendor {c['actual_value'] or '-'}")
        _field(pdf, "Why it failed", c["reason"])
        for r in c["broken_rules"]:
            pdf.ln(1)
            pdf.set_fill_color(245, 245, 240)
            pdf.font("B", 9)
            pdf.multi_cell(0, 5, pdf.clean(f"Specification: {r['id']} · § {r['section']} {r['section_title']} · page {r['page']} · "
                                           f"{r['document']}"), fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.font("", 9)
            pdf.multi_cell(0, 4.5, pdf.clean(f"„{r['quote']}“"), new_x="LMARGIN", new_y="NEXT")
            pdf.multi_cell(0, 4.5, pdf.clean(f"Rule: {r['statement']}"), new_x="LMARGIN", new_y="NEXT")
            for fl in r["flags"]:
                pdf.multi_cell(0, 4.5, pdf.clean(f"Note: {fl}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    _heading(pdf, f"2. Missing information ({len(missing) + len(report['unknown_ids'])})")
    if not missing and not report["unknown_ids"]:
        pdf.font("", 10)
        pdf.cell(0, 6, "Every test case was reported and could be judged.", new_x="LMARGIN", new_y="NEXT")
    for c in missing:
        label = "not reported" if c["status"] == "MISSING" else "unclear"
        _bar(pdf, f"{c['id']}  ·  {label}  ·  {c['title']}", (252, 241, 214))
        _field(pdf, "Vendor output", c["actual"])
        _field(pdf, "Reason", c["reason"])
        pdf.ln(2)
    if report["unknown_ids"]:
        _field(pdf, "Case ids in the vendor output that are not our test cases", ", ".join(report["unknown_ids"]))

    _heading(pdf, f"3. Passed test cases ({len(passed)})")
    pdf.font("", 9)
    for c in passed:
        pdf.multi_cell(0, 5, pdf.clean(f"{c['id']}  ·  {c['title']}"), new_x="LMARGIN", new_y="NEXT")

    _heading(pdf, "4. Summary")
    rows = []
    for t in report["topics"]:
        tc = [c for c in cases if c["topic"] == t]
        rows.append([t, len(tc), *(sum(c["status"] == s for c in tc) for s in ("PASS", "FAIL", "UNCLEAR", "MISSING"))])
    rows.append(["Total", len(cases), counts["PASS"], counts["FAIL"], counts["UNCLEAR"], counts["MISSING"]])
    _table(pdf, ["Topic", "Cases", "Passed", "Failed", "Unclear", "Missing"], rows, [40, 25, 25, 25, 25, 25])
    pdf.ln(3)
    broken = {}
    for c in failed:
        for r in c["broken_rules"]:
            broken.setdefault(r["id"], r)
    _field(pdf, "Verdict", report["verdict"], 10)
    if broken:
        _field(pdf, "Rules not met", "\n".join(f"{r['id']} (§ {r['section']}, page {r['page']}): {r['statement']}"
                                               for r in broken.values()), 10)
    next_step = {
        "CERTIFIED": "Every test case passed. The examiner can approve the certificate.",
        "REJECTED": "The vendor must fix the failed test cases above and send a new output. The examiner checks each failure first, "
                    "because the expected results are AI-drafted.",
        "MISSING INFORMATION": "Nothing failed, but the vendor must send the missing or unclear test cases before a decision.",
    }[report["verdict"]]
    _field(pdf, "Next step", next_step, 10)

    data = bytes(pdf.output())
    if path:
        path.write_bytes(data)
    return data
