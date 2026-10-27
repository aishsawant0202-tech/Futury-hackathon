import pytest

from specpilot import config, db
from specpilot.tools.pdf_ingest import Page, _can_follow, find_sections, ingest


@pytest.mark.parametrize(
    "current, candidate, ok",
    [
        ((2, 2), (2, 3), True),  # next sibling
        ((2, 3, 4, 3), (3, 1), True),  # next chapter, first section
        ((2, 2), (2, 2, 2, 1), True),  # PDF dropped 2.2.1 / 2.2.2
        ((3, 11), (3, 12), True),
        ((1,), (3, 1), False),  # list on page 16 ("3.1 DSLW ...")
        ((3, 12), (3, 7), False),  # backwards: a reference, not a heading
        ((2, 2, 2, 5), (2, 2, 2, 7), False),  # skips one
        ((3, 12), (3, 12), False),
    ],
)
def test_can_follow(current, candidate, ok):
    assert _can_follow(current, candidate) is ok


def test_find_sections_on_made_up_pages():
    pages = [
        Page(1, "2. Grundsätzliches\n2.1 Rechtlicher Hintergrund\nText zum Hintergrund"),
        # a cross-reference, a field row and a list item that only look like headings
        Page(2, "weiter mit 2.1\n2.7 „Datenbaustein DBEE“ zu verwenden\n8 Stellen linksbündig\n4.1 DSLW – Leistungswesen"),
        Page(3, "2.2 Verfahren bei den Arbeitgebern\nDie Vorerkrankungsanfrage ..."),
        Page(4, "Ende von 2.2\n2.3 Verfahren bei den Sozialversicherungsträgern\nText"),
    ]
    page_section, sections = find_sections(pages)

    assert page_section == {1: "2.1", 2: "2.1", 3: "2.2", 4: "2.2"}
    assert [(s.number, s.first_page, s.last_page) for s in sections] == [
        ("2", 1, 1),
        ("2.1", 1, 2),  # 2.2 opens page 3, so 2.1 ends on page 2
        ("2.2", 3, 4),  # 2.3 starts mid page 4, so page 4 is shared
        ("2.3", 4, 4),
    ]


@pytest.mark.skipif(not config.SPEC_PDF.exists(), reason="ITSG spec PDF not in data/specs")
def test_real_spec(tmp_path):
    conn = db.connect(tmp_path / "spec.db")
    pages, sections = ingest(conn, config.SPEC_PDF, config.SPEC_DOCUMENT)

    assert len(pages) == 168
    hits = db.search_pages(conn, "Vorerkrankungsanfrage")
    assert hits[0]["page"] == 135 and hits[0]["section"] == "3.12"
    assert db.section_pages(conn, config.SPEC_DOCUMENT, "3.12") == list(range(135, 148))
    assert db.section_pages(conn, config.SPEC_DOCUMENT, "2.2.2.5") == [27]
    # Running header is cut off, the body starts with the heading
    assert db.get_page(conn, config.SPEC_DOCUMENT, 135).startswith("3.12 Datenbaustein DBVO")
    numbers = [s.number for s in sections]
    assert numbers[numbers.index("3.1") : numbers.index("3.20") + 1] == [f"3.{i}" for i in range(1, 21)]
