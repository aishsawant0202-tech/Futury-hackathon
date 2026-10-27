import pytest

from specpilot import config, db
from specpilot.models import Requirement, RuleKind
from specpilot.tools.quote_check import compare, normalize, verify_requirement

DOC = "Test-Spezifikation"
# Text as pypdf gives it for page 135: words split at line ends, bullets, „quotes“
PAGE_135 = """Die Vorerkrankungsanfrage (3.1.18 „Abgabegrund“ = „41“) darf nur dann durch den Arbeitgeber
ausgelöst werden, wenn
• dem Arbeitgeber für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheini-
gung vorliegt,
• die kumulierten Zeiten ... mindestens
30 Tage umfassen . Das Dokument soll als Aufhänger für die K lärung dienen."""
PAGE_136 = "Sofern der Arbeitnehmer privat krankenversichert oder geringfügig beschäftigt ist, ist eine Vorerkrankungsanfrage nicht möglich."

SICK_NOTE = "dem Arbeitgeber für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheinigung vorliegt"


def test_normalize_joins_split_words_and_unifies_marks():
    assert normalize("Arbeitsunfähigkeitsbescheini-\ngung") == "arbeitsunfähigkeitsbescheinigung"
    assert normalize("„Abgabegrund“ – „41“") == '"abgabegrund" - "41"'
    assert normalize("• a\n  b") == "a b"


@pytest.mark.parametrize(
    "quote",
    [
        SICK_NOTE,
        'Die Vorerkrankungsanfrage (3.1.18 "Abgabegrund" = "41") darf nur dann',  # straight quotes
        "mindestens 30 Tage umfassen.",  # PDF has a space before the dot
        "für die Klärung dienen",  # PDF has "K lärung"
        "eine Arbeitsunfähigkeitsbescheini-gung vorliegt",  # model kept the line-break hyphen
    ],
)
def test_real_quotes_are_exact(quote):
    exact, score, _ = compare(quote, PAGE_135)
    assert exact and score == 100


def test_one_changed_word_is_not_verified():
    exact, score, match = compare(SICK_NOTE.replace(" eine ", " keine "), PAGE_135)
    assert not exact
    assert score > 90  # close, so the examiner sees it next to the real text
    assert "eine arbeitsunfähigkeitsbescheinigung" in match


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "q.db")
    db.save_page(c, DOC, 135, PAGE_135, "3.12")
    db.save_page(c, DOC, 136, PAGE_136, "3.12")
    yield c
    c.close()


def _rule(quote, page=135):
    return Requirement(id="R-3.12-001", section="3.12", page=page, quote=quote, statement="x", kind=RuleKind.PRECONDITION, document=DOC)


def test_verify_requirement_cases(conn):
    ok = verify_requirement(conn, _rule(SICK_NOTE))
    assert ok.quote_verified and ok.flags == []

    close = verify_requirement(conn, _rule(SICK_NOTE.replace(" eine ", " keine ")))
    assert not close.quote_verified and "not exact on p.135" in close.flags[0]

    wrong_page = verify_requirement(conn, _rule(PAGE_136, page=135))
    assert not wrong_page.quote_verified and wrong_page.flags == ["Quote is on p.136, not on p.135"]

    invented = verify_requirement(conn, _rule("Die Anfrage ist erst ab 42 Tagen Arbeitsunfähigkeit zulässig."))
    assert not invented.quote_verified and invented.flags[0].startswith("Quote not found")


def test_check_changes_nothing(conn):
    """Only quote_verified and flags may change; the rule's content, the spec text and the database stay as they were."""
    content = ("id", "section", "page", "quote", "statement", "kind", "document", "actor", "reason_codes")
    for quote, page in [(SICK_NOTE, 135), (SICK_NOTE.replace(" eine ", " keine "), 135), (PAGE_136, 135)]:
        before = _rule(quote, page)
        after = verify_requirement(conn, before)
        assert before.quote_verified is False and before.flags == []  # input object untouched
        assert {k: getattr(after, k) for k in content} == {k: getattr(before, k) for k in content}
    assert db.get_page(conn, DOC, 135) == PAGE_135 and db.get_page(conn, DOC, 136) == PAGE_136
    assert db.list_requirements(conn) == []  # the check never saves; the agent decides what to store


@pytest.mark.skipif(not config.SPEC_PDF.exists(), reason="ITSG spec PDF not in data/specs")
def test_real_spec_page_135_vs_140(tmp_path):
    from specpilot.tools.pdf_ingest import ingest
    from specpilot.tools.quote_check import check

    c = db.connect(tmp_path / "spec.db")
    ingest(c, config.SPEC_PDF, config.SPEC_DOCUMENT)
    assert check(c, config.SPEC_DOCUMENT, SICK_NOTE, 135).verified
    assert not check(c, config.SPEC_DOCUMENT, SICK_NOTE, 140).verified
