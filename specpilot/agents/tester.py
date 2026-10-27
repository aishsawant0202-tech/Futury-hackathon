"""Testing agent: writes test cases for the topics a reviewer selected, from the extracted rules of each topic.

Run: python -m specpilot.agents.tester                       (list the topics)
     python -m specpilot.agents.tester 3.12 2.2.2.5 [--cases 10] [--export output/test_cases]

A topic is a spec section that has extracted rules. Flow per selected topic (at most MAX_TOPICS):
  1. take the topic's testable rules: employer duty, quote verified (the extractor's output)
  2. GLM gets those rules with their German quotes and writes `cases` test cases
     (positive, negative, boundary), each citing the rule ids it tests
  3. code checks the answer: a citation of a rule outside the topic is removed, a case with no valid
     citation is dropped, every rule must be covered by at least one case. A failed check goes back to
     the model with the reason, up to `attempts` times; what is still wrong is reported, never hidden.
  4. code numbers the cases TC-<section>-001, ... and saves them (replacing the topic's old cases)
  5. export writes two files: test_cases.csv for the tester (no expected answers, no rule ids) and
     expected_results.csv (the expected answer per case, with the rules and pages it rests on)
"""
import argparse
import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from specpilot import config, db, llm
from specpilot.models import AuditEntry, CaseKind, Requirement, TopicCase
from specpilot.tools import pdf_export

log = logging.getLogger("specpilot.tester")

MAX_TOPICS = 15
DEFAULT_CASES = 10

SYSTEM = """You write test cases for payroll software that must follow a German specification
(DTA EEL, data exchange of income replacement benefits between employers and social insurance).

You get the rules of ONE topic: each rule has an id, its page, a plain English statement and the exact German quote.
Write test cases that check whether the software follows these rules:
- use ONLY the given rules; never add requirements of your own
- every case cites the id(s) of the rule(s) it tests in requirement_ids
- together the cases cover every rule at least once
- mix the kinds: positive (rule met), negative (rule violated), boundary (exactly on a limit, e.g. 30 days, 6 months)
- `given`: the concrete situation and input data (real dates, numbers, codes), so a tester can enter it
- `steps`: what the tester does in the payroll software
- `expected`: exactly what correct software must do or output, following from the cited rules
Write in English; keep German field names and terms from the quotes as they are."""


class CaseDraft(BaseModel):
    """What the model fills in. id and section are added by code."""

    model_config = ConfigDict(extra="forbid")
    requirement_ids: list[str] = Field(description="Ids of the given rules this case tests")
    kind: CaseKind
    title: str
    given: str
    steps: list[str]
    expected: str


class CaseDrafts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[CaseDraft]


class TesterError(Exception):
    __test__ = False


@dataclass
class TopicResult:
    section: str
    cases: list[TopicCase]
    uncovered: list[str] = field(default_factory=list)  # rule ids no case tests
    dropped: int = 0  # cases removed because they cited no rule of the topic


def list_topics(conn: sqlite3.Connection, document: str = config.SPEC_DOCUMENT) -> list[dict]:
    """Every section with extracted rules: number, title, testable rule count, all rule count, saved case count."""
    titles = {s["number"]: s["title"] for s in db.list_sections(conn, document)}
    counts: dict[str, dict] = {}
    for r in db.list_requirements(conn):
        t = counts.setdefault(r.section, {"number": r.section, "title": titles.get(r.section, ""), "rules": 0, "all_rules": 0})
        t["all_rules"] += 1
        t["rules"] += _testable(r)
    for t in counts.values():
        t["cases"] = len(db.list_topic_cases(conn, t["number"]))
    return sorted(counts.values(), key=lambda t: tuple(int(n) for n in t["number"].split(".")))


def _testable(r: Requirement) -> bool:
    return r.actor.value == "employer" and r.quote_verified


def topic_rules(conn: sqlite3.Connection, section: str) -> list[Requirement]:
    return [r for r in db.list_requirements(conn, actor="employer", verified=True) if r.section == section]


def _prompt(section: str, title: str, rules: list[Requirement], cases: int) -> str:
    lines = [f"Topic {section} {title}".rstrip(), "", "Rules:"]
    for r in rules:
        lines.append(f"{r.id} (p.{r.page}, {r.kind.value}): {r.statement}")
        lines.append(f'    quote: "{r.quote}"')
    lines += ["", f"Write exactly {cases} test cases."]
    return "\n".join(lines)


def check(drafts: list[CaseDraft], rule_ids: list[str]) -> tuple[list[CaseDraft], list[str], int, list[str]]:
    """Keep only citations of the topic's rules. Returns kept cases, uncovered rule ids, dropped count, problems."""
    allowed, kept, problems, dropped = set(rule_ids), [], [], 0
    for d in drafts:
        unknown = [i for i in d.requirement_ids if i not in allowed]
        if unknown:
            problems.append(f'case "{d.title}" cites {", ".join(unknown)}, which are not rules of this topic')
        ids = list(dict.fromkeys(i for i in d.requirement_ids if i in allowed))
        if not ids:
            dropped += 1
            continue
        kept.append(d.model_copy(update={"requirement_ids": ids}))
    covered = {i for d in kept for i in d.requirement_ids}
    uncovered = [i for i in rule_ids if i not in covered]
    if uncovered:
        problems.append(f"no case tests {', '.join(uncovered)}")
    return kept, uncovered, dropped, problems


def generate(
    conn: sqlite3.Connection, section: str, cases: int = DEFAULT_CASES, document: str = config.SPEC_DOCUMENT,
    model: str = config.LLM_MODEL_CODE, attempts: int = 2,
) -> TopicResult:
    rules = topic_rules(conn, section)
    if not rules:
        raise TesterError(f"topic {section} has no testable rules (employer, verified quote); run the extractor first")
    title = next((s["title"] for s in db.list_sections(conn, document) if s["number"] == section), "")
    user = _prompt(section, title, rules, cases)
    rule_ids = [r.id for r in rules]

    first_call = len(llm.calls)
    for attempt in range(attempts):
        drafts = llm.ask_json(model, SYSTEM, user, CaseDrafts).cases
        kept, uncovered, dropped, problems = check(drafts, rule_ids)
        if not problems and len(kept) >= cases:
            break
        if len(kept) < cases:
            problems.append(f"{len(kept)} valid cases instead of {cases}")
        log.warning("topic %s attempt %d: %s", section, attempt + 1, "; ".join(problems))
        user = _prompt(section, title, rules, cases) + "\n\nYour previous answer had these problems:\n- " + "\n- ".join(problems)

    result = TopicResult(
        section=section,
        cases=[
            TopicCase(id=f"TC-{section}-{n:03d}", section=section, **d.model_dump())
            for n, d in enumerate(kept[:cases], start=1)
        ],
        uncovered=uncovered,
        dropped=dropped,
    )
    db.save_topic_cases(conn, section, result.cases)
    calls = llm.calls[first_call:]
    db.log(conn, AuditEntry(
        actor="tester",
        action=f"wrote {len(result.cases)} test cases",
        subject_id=section,
        model=calls[-1]["model"] if calls else "",
        cache_hit=bool(calls) and all(c["cache_hit"] for c in calls),
        duration_s=round(sum(c["duration_s"] for c in calls), 2),
        detail=f"uncovered: {', '.join(uncovered)}" if uncovered else "",
    ))
    return result


def run(conn: sqlite3.Connection, sections: list[str], cases: int = DEFAULT_CASES, document: str = config.SPEC_DOCUMENT) -> list[TopicResult]:
    if not sections:
        raise TesterError("select at least one topic")
    if len(sections) > MAX_TOPICS:
        raise TesterError(f"{len(sections)} topics selected; at most {MAX_TOPICS}")
    known = {t["number"] for t in list_topics(conn, document)}
    unknown = [s for s in sections if s not in known]
    if unknown:
        raise TesterError(f"no extracted rules for topic(s) {', '.join(unknown)}")
    return [generate(conn, s, cases, document) for s in sections]


VENDOR_FIELDS = ["id", "topic", "title", "given", "steps"]
EXPECTED_FIELDS = ["id", "topic", "kind", "expected", "rules"]


def export_rows(conn: sqlite3.Connection, sections: list[str]) -> tuple[list[dict], list[dict]]:
    """Rows of test_cases.csv (for the vendor: no expected answers, no rule ids) and expected_results.csv."""
    cases = [c for s in sections for c in db.list_topic_cases(conn, s)]
    pages = {r.id: r.page for r in db.list_requirements(conn)}
    vendor = [{"id": c.id, "topic": c.section, "title": c.title, "given": c.given, "steps": "\n".join(c.steps)} for c in cases]
    expected = [
        {"id": c.id, "topic": c.section, "kind": c.kind.value, "expected": c.expected,
         "rules": ", ".join(f"{i} (p.{pages.get(i, '?')})" for i in c.requirement_ids)}
        for c in cases
    ]
    return vendor, expected


def export(conn: sqlite3.Connection, sections: list[str], out_dir: Path) -> tuple[Path, ...]:
    """Write test_cases and expected_results, each as .csv and .pdf, for the given topics. Returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = (out_dir / "test_cases.csv", out_dir / "expected_results.csv")
    vendor, expected = export_rows(conn, sections)
    for path, rows, fields in ((files[0], vendor, VENDOR_FIELDS), (files[1], expected, EXPECTED_FIELDS)):
        # utf-8-sig so Excel shows the German umlauts
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    pdfs = (out_dir / "test_cases.pdf", out_dir / "expected_results.pdf")
    pdf_export.test_cases_pdf(vendor, pdfs[0])
    pdf_export.expected_results_pdf(expected, pdfs[1])
    return files + pdfs


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description="Write test cases for the selected topics.")
    p.add_argument("sections", nargs="*", help=f"topics (spec sections), at most {MAX_TOPICS}; none = list the topics")
    p.add_argument("--cases", type=int, default=DEFAULT_CASES, help="test cases per topic")
    p.add_argument("--export", type=Path, help="folder for test_cases and expected_results (.csv and .pdf)")
    args = p.parse_args()
    conn = db.connect()

    if not args.sections:
        topics = list_topics(conn)
        for t in topics:
            print(f"{t['number']:<10} {t['rules']:>3} testable / {t['all_rules']:>3} rules  {t['cases']:>3} cases  {t['title']}")
        print(f"\n{len(topics)} topics" if topics else "No extracted rules yet: run python -m specpilot.agents.extractor <section>")
        return

    for res in run(conn, args.sections, args.cases):
        print(f"\n== {res.section}: {len(res.cases)} cases" + (f", {res.dropped} dropped" if res.dropped else ""))
        for c in res.cases:
            print(f"{c.id:<16} {c.kind.value:<9} [{','.join(c.requirement_ids)}] {c.title}")
        if res.uncovered:
            print(f"   ⚑ no case for {', '.join(res.uncovered)}")
    if args.export:
        for path in export(conn, args.sections, args.export):
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
