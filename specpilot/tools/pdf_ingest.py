"""Load a spec PDF into the database: one row per page, each page tagged with its section.

Run: python -m specpilot.tools.pdf_ingest [pdf] [document name]

Page numbers are the printed ones ("Seite 135 von 168"), because that is what rules cite.
The running header (section title + "Stand: ... Seite x von y ...") is cut off each page,
so it never ends up in a quote or a search hit.

Only numbered headings ("3.12 Datenbaustein DBVO ...", "2.2.2.5 Verwendungsregeln ...") are
detected. The field headings inside a section (3.12.1, 3.12.2, ...) lose their numbers in the
PDF text, so a field belongs to its parent section here.
"""
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from specpilot import config, db

PAGE_NO = re.compile(r"Seite\s+(\d+)\s+von\s+\d+")
# "3.12 Datenbaustein ..." - at least two levels, title starts with a capital letter.
# A cross-reference ('3.7 „Datenbaustein ...“ zu verwenden') starts with a quote mark, so it does not match.
HEADING = re.compile(r"^(\d+(?:\.\d+)+)\s+([A-ZÄÖÜ].*)$")
# "2. Grundsätzliches" - chapters are written with a trailing dot
CHAPTER = re.compile(r"^(\d+)\.\s+([A-ZÄÖÜ].*)$")
TOC_LEADER = re.compile(r"\.{4,}")


@dataclass
class Page:
    number: int  # printed page number
    text: str  # body without the running header


@dataclass
class Section:
    number: str
    title: str
    first_page: int
    last_page: int


def read_pages(pdf: Path) -> list[Page]:
    pages = []
    for index, raw in enumerate(PdfReader(pdf).pages, start=1):
        lines = (raw.extract_text() or "").splitlines()
        number = index
        # The header is "<section title>" then "Stand: ... Seite x von y ..." in the first lines
        for i, line in enumerate(lines[:3]):
            m = PAGE_NO.search(line)
            if m:
                number = int(m.group(1))
                lines = lines[i + 1 :]
                break
        pages.append(Page(number, "\n".join(lines).strip()))
    return pages


def _key(number: str) -> tuple[int, ...]:
    return tuple(int(n) for n in number.split("."))


def _can_follow(current: tuple[int, ...], candidate: tuple[int, ...]) -> bool:
    """Does `candidate` fit as the next heading after `current`?

    It must come later, and step forward by 1 at the first level where it differs
    (2.2 -> 2.3, 2.3.4.3 -> 3.1). Levels deeper than `current` may start at 1 or 2,
    because the PDF drops some intermediate headings (2.2 -> 2.2.2.1).
    This rejects lists and references that look like headings (1. Einführung -> "3.1 DSLW").
    """
    if candidate <= current:
        return False
    for level, value in enumerate(candidate):
        if level >= len(current):
            return all(v <= 2 for v in candidate[level:])
        if value != current[level]:
            return value == current[level] + 1 and all(v <= 2 for v in candidate[level + 1 :])
    return False


def find_sections(pages: list[Page]) -> tuple[dict[int, str], list[Section]]:
    """Walk all pages in order. Returns {page: section} and the list of sections with page ranges.

    A page's section is the one in effect at its first line of body text, so a page that
    opens with "3.12 ..." belongs to 3.12, and one that continues 3.11 belongs to 3.11.
    """
    current: tuple[int, ...] = ()
    page_section: dict[int, str] = {}
    found: list[tuple[str, str, int, bool]] = []  # number, title, page, heading opens the page

    for page in pages:
        seen_body = False
        for line in (l.strip() for l in page.text.splitlines()):
            if not line:
                continue
            m = None if TOC_LEADER.search(line) else (HEADING.match(line) or CHAPTER.match(line))
            if m and _can_follow(current, _key(m.group(1))):
                current = _key(m.group(1))
                found.append((m.group(1), m.group(2).strip(), page.number, not seen_body))
                continue
            if not seen_body:
                seen_body = True
                page_section[page.number] = ".".join(map(str, current))
        page_section.setdefault(page.number, ".".join(map(str, current)))

    last_page = pages[-1].number if pages else 0
    sections = []
    for i, (number, title, first, _) in enumerate(found):
        if i + 1 < len(found):
            _, _, next_page, next_opens_page = found[i + 1]
            end = next_page - 1 if next_opens_page else next_page
        else:
            end = last_page
        sections.append(Section(number, title, first, max(first, end)))
    return page_section, sections


def ingest(conn: sqlite3.Connection, pdf: Path, document: str) -> tuple[list[Page], list[Section]]:
    pages = read_pages(pdf)
    page_section, sections = find_sections(pages)
    for page in pages:
        db.save_page(conn, document, page.number, page.text, page_section.get(page.number, ""))
    db.save_sections(conn, document, [vars(s) for s in sections])
    return pages, sections


def main() -> None:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else config.SPEC_PDF
    document = sys.argv[2] if len(sys.argv) > 2 else config.SPEC_DOCUMENT
    conn = db.connect()
    pages, sections = ingest(conn, pdf, document)
    print(f"{document}: {len(pages)} pages, {len(sections)} sections")
    for s in sections:
        print(f"  {s.number:<10} p.{s.first_page}-{s.last_page}  {s.title[:70]}")
    print("\nSearch 'Vorerkrankungsanfrage':")
    for hit in db.search_pages(conn, "Vorerkrankungsanfrage", limit=5):
        print(f"  p.{hit['page']} (§ {hit['section']}): {hit['snippet']}")


if __name__ == "__main__":
    main()
