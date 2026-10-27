"""Extraction agent: reads a spec section and returns its rules as draft Requirements.

Run: python -m specpilot.agents.extractor [section ...]     (default: 3.12 and 2.2.2.5)

Flow per section:
  1. load the section's pages from the database (cut to the section's own text)
  2. ask Kolibri-1 for rules, three times with different page windows
     (1 page, 2 pages, whole section) - temperature 0 + cache would make three
     identical runs pointless, so the runs differ in how the text is cut
  3. quote-check every draft, merge drafts of the same rule, count votes
     (a one-page section has only one distinct cut, so its rules get at most 1 vote)
  4. number the rules in page order: R-3.12-001, ...
     A rule the model gave no reason code gets the codes its section names (§ 3.12 -> 11, 12, 41, 61).
Then across all sections GLM looks for contradictions, which become flags.

The model only reads and proposes. Code checks the quotes and the labels. Every rule is kept;
testing picks the employer rules with a verified quote, the report lists the rest with the reason.
Few-shot material comes from § 3.7 DBEE, never from the answer-key file.
"""
import logging
import re
import sqlite3
import sys

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

from specpilot import config, db, llm
from specpilot.models import REASON_CODES, Actor, AuditEntry, ReasonCode, Requirement, RuleKind
from specpilot.tools import quote_check

log = logging.getLogger("specpilot.extractor")

DEFAULT_SECTIONS = ("3.12", "2.2.2.5")
WINDOWS = (1, 2, 0)  # pages per window; 0 = whole section
EXAMPLE_PAGE = 92  # § 3.7 DBEE


class RuleDraft(BaseModel):
    """What the model fills in. id, votes, quote_verified and flags are added by code."""

    model_config = ConfigDict(extra="forbid")
    page: int = Field(description="Number from the [Seite n] marker the quote is on")
    quote: str = Field(min_length=10, description="Exact German words from that page, one sentence or bullet")
    statement: str = Field(description="The rule in one plain English sentence")
    kind: RuleKind
    actor: Actor
    reason_codes: list[ReasonCode] = Field(description="Abgabegrund codes of the messages the rule applies to; [] = every message")


class RuleDrafts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RuleDraft]


class Conflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_ids: list[str] = Field(min_length=2)
    explanation: str


class Conflicts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflicts: list[Conflict]


SYSTEM = """You extract testable rules from a German specification for payroll software
(Verfahrensbeschreibung DTA EEL). The text is given page by page, each page starts with [Seite n].

A rule is a sentence that says what must, may or must not happen:
- precondition: when a message may or must be sent at all
- content: what a field must contain, or which value to report in which case
- order: in which order repeated entries are reported
- format: length, type or value range of a field, when stated in the text

For every rule:
- quote: copy the German words from ONE page character for character. Keep brackets, references such as
  (3.12.2 „Beginn der Arbeitsunfähigkeit/Maßnahme beim Arbeitgeber“), quotation marks and numbers exactly as
  printed. Start and end the quote at a sentence or bullet boundary; never leave out words in the middle and
  never stop in the middle of a sentence. Join a word split at a line end ("Arbeitsunfähigkeitsbescheini-" +
  "gung" = "Arbeitsunfähigkeitsbescheinigung"). Never combine text from two pages.
- Two sentences that look alike but differ in one detail (a limit, a period, "nicht mehr als" vs "nicht
  mindestens") are two rules: extract both.
- page: the n of the [Seite n] marker above the quote
- statement: one plain English sentence, with the concrete numbers and values from the quote
- actor: "employer" if the employer's payroll software must do it, "insurer" if it is a duty of the
  health insurer (Krankenkasse) or SV-Träger
- reason_codes: the Abgabegrund codes (list below) of the messages the rule applies to. Use the codes named
  in the text or clear from the context: the employer's request for previous illnesses is "41", the
  insurer's answer is "61". Use [] only if the rule applies to every message.
- One rule per condition: split a list of conditions (bullets) into separate rules.

Not rules: background and purpose ("benötigen die Arbeitgeber ... um ... zu vermeiden"), references
without content ("siehe 3.7.4"), and the field layout rows (position, length, field name like
"008 n M EEL-ABAG ..."), which are read elsewhere.
Do not invent rules and do not use knowledge from outside the given pages.

Abgabegrund codes:
""" + "\n".join(f'"{code}" = {text}' for code, text in REASON_CODES.items())

EXAMPLE_ANSWER = RuleDrafts(
    rules=[
        RuleDraft(
            page=EXAMPLE_PAGE,
            quote="Maßgebend für die Meldung des Arbeitgebers (3.1.18 „Abgabegrund“ „42“) ist der Tag des Beginns der Entgeltersatzleistung.",
            statement="In the employer's request with reason 42, the field 'Beginn der Entgeltersatzleistung Arbeitgeber' holds the day the benefit started.",
            kind=RuleKind.CONTENT,
            actor=Actor.EMPLOYER,
            reason_codes=["42"],
        ),
        RuleDraft(
            page=EXAMPLE_PAGE,
            quote="Meldet der SV-Träger den DBEE ohne vorherige Anforderung durch den Arbeitgeber, ist das Feld mit „Grundstellung“ zurückzumelden.",
            statement="If the insurer sends DBEE without a prior employer request, this field is returned in its default value (Grundstellung).",
            kind=RuleKind.CONTENT,
            actor=Actor.INSURER,
            reason_codes=["62"],
        ),
        RuleDraft(
            page=EXAMPLE_PAGE,
            quote="Erfolgt eine Anforderung des Arbeitgebers (3.1.18 „Abgabegrund“ „42“) und ist der Bezug der Entgeltersatzleistung noch laufend, ist hier „99999999“ zu melden",
            statement="If the employer requested the end date (reason 42) and the benefit is still being paid, the end date field must be 99999999.",
            kind=RuleKind.CONTENT,
            actor=Actor.INSURER,
            reason_codes=["62"],
        ),
    ]
)

CONFLICT_SYSTEM = """You review rules extracted from one specification. Find pairs (or groups) of rules that
contradict each other: for the same situation they give a different limit, period, boundary or value,
so software cannot follow both. Rules that only add to each other, or apply to different actors in a
compatible way, are not contradictions. Explain each contradiction in one sentence, naming the
differing words. Return an empty list if there are none."""


# --- loading the section -------------------------------------------------------

def _cut(text: str, start_heading: str | None, end_heading: str | None) -> str:
    """Keep the text from the line starting with start_heading up to the line starting with end_heading."""
    lines = text.splitlines()
    begin, end = 0, len(lines)
    for i, line in enumerate(lines):
        if start_heading and line.strip().startswith(start_heading + " ") and begin == 0:
            begin = i
        if end_heading and line.strip().startswith(end_heading + " "):
            end = i
            break
    return "\n".join(lines[begin:end]).strip()


def section_text(conn: sqlite3.Connection, document: str, section: str) -> list[tuple[int, str]]:
    """[(page, text)] of a section, with the first and last page cut to the section's own text."""
    pages = db.section_pages(conn, document, section)
    if not pages:
        raise ValueError(f"§ {section} not found in {document}; run pdf_ingest first")
    sections = db.list_sections(conn, document)
    numbers = [s["number"] for s in sections]
    # The next heading that is not a subsection ends this section
    after = [s for s in sections[numbers.index(section) + 1 :] if not s["number"].startswith(section + ".")]
    next_heading = after[0]["number"] if after and after[0]["first_page"] <= pages[-1] else None

    result = []
    for p in pages:
        text = db.get_page(conn, document, p) or ""
        text = _cut(text, section if p == pages[0] else None, next_heading if p == pages[-1] else None)
        result.append((p, text))
    return result


# „Abgabegrund“ = „41“ / Abgabegrund „11“, „12“ / („ABGABEGRUND“ = „41“) - the codes right after the word
_NAMED_CODES = re.compile(r"Abgabegrund\W{0,4}(?:\s*=)?((?:\s*[„\"]\d{2}[“\"]\s*(?:,|und|oder|bzw\.)?)+)", re.IGNORECASE)


def section_reason_codes(conn: sqlite3.Connection, document: str, section: str) -> list[str]:
    """The reason codes a section's own text names, e.g. § 3.12 -> 11, 12, 41, 61.

    Used for rules where the model found no code: they apply to the messages of their section.
    The official table (Anlage 3) is not in the documents we have, so this reads the section instead.
    A section naming most codes (the header record § 3.1) is a general one: [] = every message.
    """
    text = " ".join(t for _, t in section_text(conn, document, section))
    text = re.sub(r"-\s*\n\s*", "", text).replace("\n", " ")
    codes = {c for m in _NAMED_CODES.finditer(text) for c in re.findall(r"\d{2}", m.group(1)) if c in REASON_CODES}
    if len(codes) >= len(REASON_CODES) / 2:
        return []
    return sorted(codes)


def windows(pages: list[tuple[int, str]], size: int) -> list[list[tuple[int, str]]]:
    if size <= 0 or size >= len(pages):
        return [pages]
    return [pages[i : i + size] for i in range(0, len(pages), size)]


def _render(section: str, pages: list[tuple[int, str]]) -> str:
    return f"Abschnitt § {section}\n\n" + "\n\n".join(f"[Seite {p}]\n{text}" for p, text in pages)


# --- asking the model --------------------------------------------------------------

def _messages_for(conn: sqlite3.Connection, document: str) -> str:
    """System prompt plus the worked example from § 3.7 (page 92)."""
    example_page = db.get_page(conn, document, EXAMPLE_PAGE) or ""
    return (
        f"{SYSTEM}\n\nEXAMPLE\nInput:\n{_render('3.7', [(EXAMPLE_PAGE, example_page)])}\n\n"
        f"Output:\n{EXAMPLE_ANSWER.model_dump_json(indent=1)}"
    )


def ask(system: str, section: str, window: list[tuple[int, str]], model: str) -> list[RuleDraft]:
    try:
        return llm.ask_json(model, system, _render(section, window), RuleDrafts).rules
    except ValueError as err:
        # One window failing must not stop the run; the other windows still cover these pages
        log.warning("§ %s p.%s: %s", section, [p for p, _ in window], err)
        return []


# --- merging ---------------------------------------------------------------------

def _same_rule(a: RuleDraft, b: RuleDraft) -> bool:
    if a.page != b.page:
        return False
    qa, qb = quote_check.normalize(a.quote), quote_check.normalize(b.quote)
    return qa in qb or qb in qa or fuzz.token_set_ratio(qa, qb) >= 90


def merge(runs: list[list[tuple[RuleDraft, bool]]]) -> list[tuple[RuleDraft, int, bool]]:
    """Group drafts of the same rule across runs. Returns (best draft, votes, verified)."""
    clusters: list[list[tuple[int, RuleDraft, bool]]] = []
    for run_no, drafts in enumerate(runs):
        for draft, ok in drafts:
            for cluster in clusters:
                if _same_rule(cluster[0][1], draft):
                    cluster.append((run_no, draft, ok))
                    break
            else:
                clusters.append([(run_no, draft, ok)])

    merged = []
    for cluster in clusters:
        verified = [c for c in cluster if c[2]]
        _, best, ok = (verified or cluster)[0]
        merged.append((best, len({run_no for run_no, _, _ in cluster}), ok))
    return merged


# --- coverage: sentences no rule covers ------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.;:])\s+(?=[A-ZÄÖÜ„(])|\s*•\s*")  # each bullet is its own condition
_LAYOUT_LINE = re.compile(
    r"^(\d{3}(-\d{3})?(\s|-?$)"  # field rows "008 n M EEL-ABAG ...", positions "005-", "012"
    r"|\d+(\.\d+)+\s"  # headings "3.12 Datenbaustein ..."
    r"|[^aeiouäöüAEIOUÄÖÜ\s]+$)"  # format masks "jhjjmmtt", "nnnnnnn"
)


def sentences(text: str) -> list[str]:
    """Running text of a page split into sentences and bullets. Layout rows and headings are dropped."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    lines = [line for line in text.splitlines() if line.strip() and not _LAYOUT_LINE.match(line.strip())]
    result = []
    for s in _SENTENCE_END.split(" ".join(lines)):
        s = re.sub(r"\s+", " ", s).strip()
        words = s.split()
        if len(words) >= 4 and sum(w[:1].isalpha() for w in words) / len(words) >= 0.7:
            result.append(s)
    return result


def uncovered(pages: list[tuple[int, str]], quotes_by_page: dict[int, list[str]]) -> dict[int, list[str]]:
    """{page: sentences that no rule quote on that page covers}."""
    result = {}
    for page, text in pages:
        quotes = [quote_check.compact(q) for q in quotes_by_page.get(page, [])]
        missing = []
        for s in sentences(text):
            cs = quote_check.compact(s)
            if not any(q in cs or cs in q or fuzz.partial_ratio(q, cs) >= 90 for q in quotes):
                missing.append(s)
        if missing:
            result[page] = missing
    return result


def _position(conn: sqlite3.Connection, document: str, draft: RuleDraft) -> tuple[int, int]:
    page = quote_check.normalize(db.get_page(conn, document, draft.page) or "").replace(" ", "")
    pos = page.find(quote_check.normalize(draft.quote).replace(" ", ""))
    return draft.page, pos if pos >= 0 else 10**6


# --- the agent ---------------------------------------------------------------------

def _audit(conn: sqlite3.Connection, first_call: int, action: str, subject: str) -> None:
    """One row per step, summing the model calls it made (ask_json may retry)."""
    calls = llm.calls[first_call:]
    db.log(conn, AuditEntry(
        actor="extractor",
        action=action,
        subject_id=subject,
        model=calls[-1]["model"] if calls else "",
        cache_hit=bool(calls) and all(c["cache_hit"] for c in calls),
        duration_s=round(sum(c["duration_s"] for c in calls), 2),
    ))


def extract(conn: sqlite3.Connection, section: str, document: str = config.SPEC_DOCUMENT, model: str = config.LLM_MODEL_GERMAN) -> list[Requirement]:
    pages = section_text(conn, document, section)
    system = _messages_for(conn, document)

    runs, seen = [], set()
    for size in WINDOWS:
        split = windows(pages, size)
        layout = tuple(tuple(p for p, _ in w) for w in split)
        if layout in seen:
            continue  # same cut as an earlier run (short section): it would only repeat the cached answer as an extra vote
        seen.add(layout)
        drafts = []
        for window in split:
            first_call = len(llm.calls)
            found = ask(system, section, window, model)
            _audit(conn, first_call, f"extract p.{window[0][0]}-{window[-1][0]} (window {size or 'all'}): {len(found)} rules", section)
            drafts += [(d, quote_check.check(conn, document, d.quote, d.page).verified) for d in found]
        runs.append(drafts)

    # Coverage pass: sentences no run turned into a rule go back to the model once, page by page
    def quotes_by_page() -> dict[int, list[str]]:
        result: dict[int, list[str]] = {}
        for drafts in runs:
            for d, _ in drafts:
                result.setdefault(d.page, []).append(d.quote)
        return result

    gaps = uncovered(pages, quotes_by_page())
    gap_drafts = []
    for page, missing in gaps.items():
        first_call = len(llm.calls)
        found = ask(system, section, [(page, "\n".join(missing))], model)
        _audit(conn, first_call, f"coverage pass p.{page}: {len(missing)} uncovered sentences -> {len(found)} rules", section)
        gap_drafts += [(d, quote_check.check(conn, document, d.quote, d.page).verified) for d in found]
    # Only new rules: the coverage pass is a catch-up, not another independent reading, so it adds no votes
    gap_drafts = [g for g in gap_drafts if not any(_same_rule(g[0], d) for drafts in runs for d, _ in drafts)]
    if gap_drafts:
        runs.append(gap_drafts)
    still = uncovered(pages, quotes_by_page())
    db.log(conn, AuditEntry(
        actor="extractor",
        action=f"coverage: {sum(map(len, still.values()))} sentences not turned into rules",
        subject_id=section,
        detail="\n".join(f"p.{p}: {s}" for p, ss in still.items() for s in ss),
    ))

    merged = sorted(merge(runs), key=lambda m: _position(conn, document, m[0]))
    section_codes = section_reason_codes(conn, document, section)
    rules = []
    for n, (draft, votes, _) in enumerate(merged, start=1):
        rule = Requirement(
            id=f"R-{section}-{n:03d}",
            section=section,
            document=document,
            votes=votes,
            **(draft.model_dump() | {"reason_codes": draft.reason_codes or section_codes}),
        )
        rules.append(quote_check.verify_requirement(conn, rule))
    return rules


_LIMIT = re.compile(r"(\d+)\s*(monat|tag|woche|jahr|stunde)", re.IGNORECASE)
GROUP_SIZE = 10  # rules per contradiction call; ~100 rules in one call ran past the gateway timeout


def conflict_groups(rules: list[Requirement], size: int = GROUP_SIZE) -> list[list[Requirement]]:
    """Rules that could contradict each other: they name the same limit ("6 Monate") or are worded alike.

    Code picks the candidates, so the model only ever compares a handful of rules at a time.
    """
    limits = [{(n, unit.lower()) for n, unit in _LIMIT.findall(r.quote)} for r in rules]
    texts = [quote_check.normalize(r.quote) for r in rules]
    parent = list(range(len(rules)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            if limits[i] & limits[j] or fuzz.token_set_ratio(texts[i], texts[j]) >= 70:
                parent[root(i)] = root(j)

    clusters: dict[int, list[Requirement]] = {}
    for i, r in enumerate(rules):
        clusters.setdefault(root(i), []).append(r)
    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (r.section, r.page))
        groups += [members[k : k + size] for k in range(0, len(members), size) if len(members[k : k + size]) >= 2]
    return groups


def find_conflicts(conn: sqlite3.Connection, rules: list[Requirement], model: str = config.LLM_MODEL_CODE) -> list[Requirement]:
    """Ask GLM, group by group, which rules contradict each other; flag every rule involved."""
    by_id = {r.id: r for r in rules}
    subject = ",".join(sorted({r.section for r in rules}))
    for group in conflict_groups(rules):
        listing = "\n".join(
            f"{r.id} (p.{r.page}, {r.actor.value}, Abgabegrund {','.join(r.reason_codes) or 'alle'}): „{r.quote}“ — {r.statement}" for r in group
        )
        first_call = len(llm.calls)
        try:
            found = llm.ask_json(model, CONFLICT_SYSTEM, listing, Conflicts).conflicts
        except ValueError as err:
            log.warning("contradiction check failed for %s: %s", [r.id for r in group], err)
            found = []
        _audit(conn, first_call, f"contradiction check {group[0].id}..{group[-1].id} ({len(group)} rules): {len(found)} found", subject)

        group_ids = {r.id for r in group}
        for c in found:
            ids = [i for i in dict.fromkeys(c.rule_ids) if i in group_ids]
            if len(ids) < 2:
                continue  # model named a rule that is not in this group
            for i in ids:
                others = ", ".join(o for o in ids if o != i)
                by_id[i] = by_id[i].model_copy(update={"flags": by_id[i].flags + [f"Contradiction with {others}: {c.explanation}"]})
    return list(by_id.values())


def run(conn: sqlite3.Connection, sections: tuple[str, ...] = DEFAULT_SECTIONS, document: str = config.SPEC_DOCUMENT) -> list[Requirement]:
    rules = []
    for section in sections:
        rules += extract(conn, section, document)
    rules = find_conflicts(conn, rules)
    for section in sections:
        db.delete_section(conn, section)
    for r in rules:
        db.save_requirement(conn, r)
    db.log(conn, AuditEntry(actor="extractor", action=f"saved {len(rules)} draft rules", subject_id=",".join(sections)))
    return rules


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    sections = tuple(sys.argv[1:]) or DEFAULT_SECTIONS
    rules = run(db.connect(), sections)
    for r in rules:
        mark = "✔" if r.quote_verified else "✘"
        codes = ",".join(r.reason_codes) or "all"
        print(f"{r.id:<16} p.{r.page} {mark} votes={r.votes} {r.kind.value:<12} {r.actor.value:<8} [{codes}] {r.statement}")
        for f in r.flags:
            print(f"{'':>18}⚑ {f}")
    calls = llm.calls
    print(f"\n{len(rules)} rules, {sum(r.quote_verified for r in rules)} quotes verified, "
          f"{sum(bool(r.flags) for r in rules)} flagged; {len(calls)} model calls ({sum(c['cache_hit'] for c in calls)} from cache)")


if __name__ == "__main__":
    main()
