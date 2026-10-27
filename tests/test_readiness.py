"""Readiness check: each check says TO DO until its material is really there, then READY."""
import pytest

from specpilot import db, readiness
from specpilot.tools import facts
from tests.test_rule_picker import GOLD_RULES


def test_key(tmp_path):
    assert not readiness.check_key(tmp_path).ready
    (tmp_path / ".env").write_text("LLM_API_KEY=put-the-team-key-here\n", encoding="utf-8")
    assert "example value" in readiness.check_key(tmp_path).detail
    (tmp_path / ".env").write_text("# comment\nLLM_API_KEY=abc123\n", encoding="utf-8")
    assert readiness.check_key(tmp_path).ready


def test_pdf(tmp_path):
    assert not readiness.check_pdf(tmp_path / "spec.pdf").ready
    (tmp_path / "spec.pdf").write_bytes(b"%PDF")
    assert readiness.check_pdf(tmp_path / "spec.pdf").ready


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "team.db")
    yield c
    c.close()


def test_spec_and_rules(conn):
    assert not readiness.check_spec_loaded(conn).ready and not readiness.check_rules(conn).ready
    db.save_page(conn, "Spec", 135, "Seite mit Text", "3.12")
    assert readiness.check_spec_loaded(conn).ready
    example = GOLD_RULES[0].model_copy(update={"quote": "[BEISPIEL] " + GOLD_RULES[0].quote})
    db.save_requirement(conn, example)
    assert "EXAMPLE rules" in readiness.check_rules(conn).detail
    db.save_requirement(conn, GOLD_RULES[0])
    assert readiness.check_rules(conn).ready


def test_without_a_database():
    assert not readiness.check_spec_loaded(None).ready and not readiness.check_rules(None).ready
    assert not readiness.check_cards(None).ready
    assert "spec is not loaded" in readiness.check_hint_words(None).detail


def test_hint_words_are_looked_up_in_the_spec(conn):
    db.save_page(conn, "Spec", 135, "nur Text ohne die Wörter", "3.12")
    c = readiness.check_hint_words(conn)
    assert not c.ready and "not found in the spec" in c.detail
    every_term = " ".join(t for f in facts.FACTS.values() for t in f.german_terms)
    db.save_page(conn, "Spec", 136, every_term, "3.12")
    assert readiness.check_hint_words(conn).ready


def test_gold_and_content_placeholders_are_still_to_do():
    """True today: the gold card has placeholder ids and the content card placeholder sources."""
    assert not readiness.check_gold().ready and "7 placeholder" in readiness.check_gold().detail
    c = readiness.check_content_sources(None)
    assert not c.ready and "14 placeholder source(s), 4 open question(s)" in c.detail


def test_cards(conn):
    from specpilot import card_store
    from specpilot.review import approve
    from tests.test_review import RULES, draft
    for r in GOLD_RULES:
        db.save_requirement(conn, r)
    assert "no approved card" in readiness.check_cards(conn).detail
    card_store.save(conn, approve(draft(), "A", RULES))
    assert readiness.check_cards(conn).ready
    db.save_requirement(conn, GOLD_RULES[2].model_copy(update={"quote": "Die Zeiten wurden geändert und neu gefasst."}))
    assert "out of date" in readiness.check_cards(conn).detail


def test_examples(tmp_path):
    assert readiness.check_examples(tmp_path).ready
    (tmp_path / "data" / "examples").mkdir(parents=True)
    assert "data/examples" in readiness.check_examples(tmp_path).detail


def test_command_exit_code_and_lines(tmp_path, capsys):
    assert readiness.main(["--db", str(tmp_path / "none.db")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("READINESS: ") and "[TO DO] gold card" in out and "->" in out
