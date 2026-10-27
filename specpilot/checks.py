"""Hand-over checks: the administrative agent runs these between the pipeline steps. Plain code, no model.

Each check reads what a step produced and returns a list of Problems; an empty list means the next step may start.
    error    the next step must not start: its input is wrong or missing
    warning  the run goes on; the problem is listed in the job's report

The steps after the translator already refuse bad input themselves (branch maker, self-check, the writers). These
checks cover what is left: the steps that only log their doubts (pdf_ingest, extractor), that no rule is lost between
extractor, rule picker and translator, the approval that replaces the examiner, and the verification agent's result.

    after_ingest      every PDF page loaded exactly once, the sections found, no page lost to text extraction
    after_extract     rules for every section, enough verified quotes, sentences no rule covers
    stale_cards       stored cards built on rules that changed since (the extractor ran again)
    after_pick        every rule about the message is picked, or left out with a reason
    after_translate   the card is built from exactly the picked rules, each one used or explained
    auto_approve      approves a card only if every automatic check passes (replaces the examiner)
    before_branches   the card is approved, stored and not out of date
    before_verify     the vendor reports can be read, the expected results of this job's dummy file are there
    after_verify      verdict, counts and cases with missing information recomputed from report and expected results
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pypdf import PdfReader

from specpilot import card_store, config, db
from specpilot.agents import extractor, verification
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.models import Actor, Requirement
from specpilot.review import ReviewError, approve, guard_errors, refresh_guards
from specpilot.tools import gold, pdf_ingest
from specpilot.tools.rule_picker import Pick

MIN_PAGE_CHARS = 20  # less body text than this: the page is blank or its text could not be read
MIN_VERIFIED = 0.8  # share of a section's rules whose quote was found on its page
MAX_UNCOVERED = 0.25  # share of a section's sentences that no rule covers
AUTO_REVIEWER = "admin agent (automatic approval)"
SHOW = 5  # ids listed in one message before "..."


@dataclass
class Problem:
    severity: Literal["error", "warning"]
    message: str

    def text(self) -> str:
        return f"[{self.severity.upper()}] {self.message}"


def error(message: str) -> Problem:
    return Problem("error", message)


def warning(message: str) -> Problem:
    return Problem("warning", message)


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == "error"]


def _ids(items) -> str:
    items = list(items)
    return ", ".join(str(i) for i in items[:SHOW]) + (f" ... ({len(items)} in total)" if len(items) > SHOW else "")


# --- pdf_ingest -> extractor ---------------------------------------------------------------------------------------

def pdf_texts(pdf: Path) -> list[str]:
    """The raw text of every PDF page, in file order, as pypdf reads it (header included)."""
    return [page.extract_text() or "" for page in PdfReader(pdf).pages]


def _printed_number(raw: str, index: int) -> int:
    """The printed page number, found the same way pdf_ingest finds it; the file position if there is none."""
    for line in raw.splitlines()[:3]:
        m = pdf_ingest.PAGE_NO.search(line)
        if m:
            return int(m.group(1))
    return index


def after_ingest(conn: sqlite3.Connection, document: str, sections: tuple[str, ...], raw_pages: list[str]) -> list[Problem]:
    """raw_pages: pdf_texts() of the PDF that was loaded, to compare the database with."""
    stored = {r["page"]: r["text"] for r in conn.execute("SELECT page, text FROM spec_pages WHERE document = ?", (document,))}
    if not stored:
        return [error(f"no pages of {document} in the database")]
    problems = []

    printed = [_printed_number(raw, i) for i, raw in enumerate(raw_pages, start=1)]
    twice = sorted({n for n in printed if printed.count(n) > 1})
    if twice:
        problems.append(error(f"PDF pages read as the same printed page number, one overwrote the other: {_ids(twice)}"))
    missing = sorted(set(printed) - set(stored))
    if missing:
        problems.append(error(f"PDF pages not in the database: {_ids(missing)}"))
    if len(stored) != len(raw_pages):
        problems.append(error(f"the PDF has {len(raw_pages)} pages, the database {len(stored)}"))

    section_pages = {}
    for s in sections:
        pages = db.section_pages(conn, document, s)
        if pages:
            section_pages.update({p: s for p in pages})
        else:
            problems.append(error(f"§ {s} was not found among the headings; its rules cannot be extracted"))

    raw_by_number = dict(zip(printed, raw_pages))
    blank, unreadable = [], []
    for page, text in sorted(stored.items()):
        if len(text.strip()) >= MIN_PAGE_CHARS:
            continue
        if page in section_pages:
            problems.append(error(f"p.{page} in § {section_pages[page]} has no text: rules on it cannot be extracted"))
        elif not raw_by_number.get(page, "").strip():
            unreadable.append(page)  # not even the running header: the page is an image, or extraction failed
        else:
            blank.append(page)  # the running header was there: a page left blank in the document
    if unreadable:
        problems.append(error(f"no text could be read from p. {_ids(unreadable)} (a scanned image?)"))
    if blank:
        problems.append(warning(f"blank pages (only the running header): p. {_ids(blank)}"))
    return problems


# --- extractor -> rule_picker ------------------------------------------------------------------------------------------

def after_extract(conn: sqlite3.Connection, sections: tuple[str, ...], reason_code: str,
                  document: str = config.SPEC_DOCUMENT) -> list[Problem]:
    problems = []
    every = db.list_requirements(conn)
    for s in sections:
        rules = [r for r in every if r.section == s]
        if not rules:
            problems.append(error(f"§ {s}: the extractor found no rules"))
            continue
        verified = sum(r.quote_verified for r in rules)
        if verified / len(rules) < MIN_VERIFIED:
            problems.append(error(f"§ {s}: only {verified} of {len(rules)} quotes were found on their page "
                                  f"(at least {MIN_VERIFIED:.0%} needed); the extraction is not reliable"))

        pages = extractor.section_text(conn, document, s)
        quotes: dict[int, list[str]] = {}
        for r in rules:
            quotes.setdefault(r.page, []).append(r.quote)
        total = sum(len(extractor.sentences(text)) for _, text in pages)
        left = sum(len(x) for x in extractor.uncovered(pages, quotes).values())
        if total and left / total > MAX_UNCOVERED:
            problems.append(warning(f"§ {s}: {left} of {total} sentences are not covered by any rule; "
                                    f"some may be rules the extractor missed"))

    testable = db.list_requirements(conn, reason_code=reason_code, actor=Actor.EMPLOYER.value, verified=True)
    if not testable:
        problems.append(error(f"no employer rule with a verified quote for reason {reason_code}: nothing to test"))
    flagged = [r.id for r in every if r.section in sections and r.flags]
    if flagged:
        problems.append(warning(f"{len(flagged)} rule(s) flagged as contradicting another rule: {_ids(flagged)}"))
    return problems


def stale_cards(conn: sqlite3.Connection) -> list[Problem]:
    """Stored criteria cards that rest on rules that changed or disappeared: they must be translated again."""
    return [warning(f"{e.card_id} v{e.version} ({e.status}) rests on rules that changed since: "
                    f"{_ids(e.changed_rules)}; it must be translated again")
            for e in card_store.entries(conn) if e.kind == "criteria" and e.stale]


# --- rule_picker -> translator --------------------------------------------------------------------------------------

def after_pick(conn: sqlite3.Connection, pick: Pick) -> list[Problem]:
    """Nothing gets lost: every rule about the message is either picked or left out with a reason."""
    every = db.list_requirements(conn)
    code = pick.spec.reason_code
    about = {r.id for r in every if not r.reason_codes or code in r.reason_codes}
    picked = [r.id for r in pick.picked]
    excluded = [e.rule.id for e in pick.excluded]
    problems = []

    lost = about - set(picked) - set(excluded)
    if lost:
        problems.append(error(f"rules about reason {code} neither picked nor left out: {_ids(sorted(lost))}"))
    foreign = (set(picked) | set(excluded)) - about
    if foreign:
        problems.append(error(f"rules not about reason {code} were considered: {_ids(sorted(foreign))}"))
    twice = sorted({i for i in picked + excluded if (picked + excluded).count(i) > 1})
    if twice:
        problems.append(error(f"rules both picked and left out, or listed twice: {_ids(twice)}"))
    silent = [e.rule.id for e in pick.excluded if not e.reasons]
    if silent:
        problems.append(error(f"rules left out without a reason: {_ids(silent)}"))
    if pick.other_messages != len(every) - len(about):
        problems.append(error(f"{len(every)} rules in the database, {len(about)} about reason {code}, but "
                              f"{pick.other_messages} counted as other messages"))

    once = [r.id for r in pick.picked if r.votes <= 1]
    if once:
        problems.append(warning(f"picked rules found in only one extraction reading: {_ids(once)}"))
    return problems


# --- translator -> approval -----------------------------------------------------------------------------------------------

def after_translate(card: CriteriaCard, pick: Pick) -> list[Problem]:
    """The card is built from exactly the picked rules, and each one is used, unused with a reason, or a missing fact."""
    problems = []
    if card.id != pick.spec.card_id:
        problems.append(error(f"the card is {card.id}, but the rules were picked for {pick.spec.card_id}"))
    lost = sorted(set(pick.rule_ids) - set(card.source_rule_ids))
    added = sorted(set(card.source_rule_ids) - set(pick.rule_ids))
    if lost:
        problems.append(error(f"picked rules missing from the card: {_ids(lost)}"))
    if added:
        problems.append(error(f"the card cites rules that were not picked: {_ids(added)}"))

    used = {rid for c in card.criteria for cond in (c, *c.alternatives) for rid in cond.source_rule_ids}
    explained = used | {u.rule_id for u in card.unused_rules} | {rid for m in card.missing_facts for rid in m.rule_ids}
    silent = [rid for rid in card.source_rule_ids if rid not in explained]
    if silent:
        problems.append(error(f"picked rules neither used nor explained on the card: {_ids(silent)}"))
    if card.unused_rules:
        problems.append(warning(f"{len(card.unused_rules)} picked rule(s) not turned into a condition: "
                                + "; ".join(f"{u.rule_id} ({u.reason})" for u in card.unused_rules)))
    if card.missing_facts:
        problems.append(error(f"{len(card.missing_facts)} condition(s) need a fact the fact dictionary lacks: "
                              + "; ".join(m.needed for m in card.missing_facts)))
    return problems


def auto_approve(card: CriteriaCard, rules: list[Requirement]) -> tuple[CriteriaCard | None, list[Problem]]:
    """Approve the card in the examiner's place, only if no automatic check has a doubt left.

    Refused when the card has missing facts or placeholder rule ids, when a guard finds the card does not match the
    German quote, or when the cross-check (the same rules translated a second time in reverse order) came out
    different or failed. Returns (approved card, []) or (None, the reasons).
    """
    if card.status is not CardStatus.DRAFT:
        return None, [error(f"{card.id} v{card.version} is {card.status.value}, only a draft can be approved")]
    problems = []
    if card.missing_facts:
        problems.append(error("the card has missing facts"))
    placeholders = gold.placeholder_ids(card)
    if placeholders:
        problems.append(error(f"placeholder rule ids: {_ids(placeholders)}"))
    known = {r.id for r in rules}
    unknown = [rid for rid in card.source_rule_ids if rid not in known]
    if unknown:
        problems.append(error(f"rules not in the database, the card cannot be checked: {_ids(unknown)}"))
    else:
        problems += [error(f"guard: {g}") for g in guard_errors(refresh_guards(card, rules))]
    problems += [error(f) for f in card.flags if f.startswith("Cross-check")]
    if problems:
        return None, problems

    note = "automatic: no missing facts, no placeholder ids, no guard errors, the cross-check agrees"
    try:
        return approve(card, AUTO_REVIEWER, rules, note=note), []
    except ReviewError as err:
        return None, [error(str(err))]


def before_branches(conn: sqlite3.Connection, card: CriteriaCard) -> list[Problem]:
    """The branch maker checks this too, but only when it is given the database: here it is never skipped."""
    if not card.approval_valid:
        return [error(f"{card.id} v{card.version} is not approved, or changed after approval")]
    try:
        changed = card_store.out_of_date(conn, card)
    except card_store.StoreError as err:
        return [error(str(err))]
    if changed:
        return [error(f"{card.id} v{card.version} is out of date, rules changed since: {_ids(changed)}")]
    return []


# --- dummy files -> verification ------------------------------------------------------------------------------------------

def before_verify(reports: list[Path], expected_dir: Path, dummy_stem: str) -> list[Problem]:
    """reports: the vendor PDFs to verify. dummy_stem: the dummy file this job wrote, e.g. REQ-41-PRECONDITION-v1-seed4711."""
    problems = []
    if not reports:
        problems.append(error("no vendor report given"))
    for path in reports:
        r = verification.read_report(Path(path))
        if not r.readable:
            problems.append(warning(f"{r.path.name}: {r.problem}"))
    if not (Path(expected_dir) / f"{dummy_stem}.expected.sqlite").exists():
        problems.append(error(f"no expected results for {dummy_stem} in {expected_dir}"))
    if not (Path(expected_dir) / f"{dummy_stem}.dbvo.sqlite").exists():
        problems.append(warning(f"no DBVO answers for {dummy_stem}: only whether the message is sent will be checked, "
                                f"not its fields"))
    return problems


def after_verify(v: verification.Verification, report: Path, expected_dir: Path, dummy_stem: str) -> list[Problem]:
    """Recompute the counts and the verdict from the vendor report and the expected results; trust nothing in v."""
    problems = []
    tested = v.dummy_file.removesuffix(".sqlite")
    if tested and tested != dummy_stem:
        problems.append(error(f"{v.report}: the vendor tested {tested}, this job made {dummy_stem}; "
                              f"the report was compared with another job's answers"))

    parsed = verification.read_vendor_report(Path(report))
    expected, _ = verification.load_expected(tested, expected_dir) if tested else ({}, [])
    compared = set(expected) & set(parsed.cases)
    if v.cases_expected != len(expected) or v.cases_compared != len(compared):
        problems.append(error(f"{v.report}: {v.cases_compared} of {v.cases_expected} cases compared, recomputed "
                              f"{len(compared)} of {len(expected)}"))
    wrong = {d.case for d in v.differences}
    if v.matches + len(wrong) != v.cases_compared:
        problems.append(error(f"{v.report}: {v.matches} matches + {len(wrong)} cases with differences "
                              f"!= {v.cases_compared} compared"))
    gaps = " ".join(m.what for m in v.missing_information)
    not_returned = sorted(set(expected) - set(parsed.cases))
    unflagged = [n for n in not_returned if n not in gaps]
    if unflagged:
        problems.append(error(f"{v.report}: cases not returned by the vendor and not flagged: {_ids(unflagged)}"))
    if v.missing_cases != not_returned:
        problems.append(error(f"{v.report}: {len(v.missing_cases)} case(s) counted as missing information, "
                              f"recomputed {len(not_returned)}"))

    verdict = (verification.REJECTED if v.differences
               else verification.MISSING_INFORMATION if v.missing_information else verification.CERTIFIED)
    if v.verdict != verdict:
        problems.append(error(f"{v.report}: verdict {v.verdict}, the flags say {verdict}"))
    if v.verdict == verification.CERTIFIED and not compared:
        problems.append(error(f"{v.report}: certified without comparing a single case"))
    for d in v.differences:
        if d.expected == d.vendor:
            problems.append(error(f"{v.report} {d.flag}: flagged as a difference, but both say {d.expected!r}"))
        if not d.reason:
            problems.append(warning(f"{v.report} {d.flag}: no rule given for why {d.expected!r} is right"))
    return problems
