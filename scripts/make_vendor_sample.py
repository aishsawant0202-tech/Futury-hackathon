"""Builds an invented vendor output PDF from our test cases, to try the validation agent without a real vendor.

Run: python -m scripts.make_vendor_sample [topic ...] [--out output/vendor/SAMPLE-vendor-output.pdf]

Every case gets its expected result as the vendor output, except: the first case gets a wrong output (a failure)
and the last case is left out (missing). The PDF says SAMPLE on every page.
"""
import argparse
from pathlib import Path

from fpdf import FPDF

from specpilot import config, db

OUT = config.ROOT / "output" / "vendor" / "SAMPLE-vendor-output.pdf"
WRONG = "No message was created. The payroll software rejected the input without any output."


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("topics", nargs="*", help="topics to include; none = every topic with test cases")
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()
    cases = [c for c in db.list_topic_cases(db.connect()) if not args.topics or c.section in args.topics]
    if not cases:
        raise SystemExit("no test cases: generate them first")

    pdf = FPDF()
    pdf.add_font("body", "", "C:/Windows/Fonts/arial.ttf")
    pdf.add_page()
    pdf.set_font("body", "", 10)
    pdf.multi_cell(0, 5, "SAMPLE (invented vendor output)\nVendor: Sample Vendor\nSoftware: Sample Payroll\nVersion: 1.0\n",
                   new_x="LMARGIN", new_y="NEXT")
    for i, c in enumerate(cases[:-1] if len(cases) > 1 else cases):
        pdf.multi_cell(0, 5, f"{c.id} | {WRONG if i == 0 else c.expected}\n", new_x="LMARGIN", new_y="NEXT")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(args.out))
    print(f"wrote {args.out}: {len(cases)} cases, 1 wrong, {1 if len(cases) > 1 else 0} left out")


if __name__ == "__main__":
    main()
