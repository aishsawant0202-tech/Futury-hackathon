"""Card store: permanent versions, status updates, out-of-date detection, export, and the branch maker's check."""
import json

import pytest

from specpilot import card_store, content_review, db, dbvo
from specpilot.card_store import StoreError
from specpilot.criteria import CriteriaCard
from specpilot.review import approve, edit_criterion, save_card
from specpilot.tools import branch_maker
from tests.test_review import RULES, draft
from tests.test_rule_picker import GOLD_RULES, NOISE


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "team.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def change_rule(conn, rid, **update):
    db.save_requirement(conn, db.get_requirement(conn, rid).model_copy(update=update))


# --- storing ----------------------------------------------------------------------------------------------

def test_store_and_load(conn):
    d = draft()
    assert card_store.save(conn, d) == "stored"
    assert card_store.load(conn, d.id, 1) == d
    assert card_store.save(conn, d) == "unchanged"


def test_status_may_move_on_content_may_not(conn):
    d = draft()
    card_store.save(conn, d)
    a = approve(d, "Examiner A", RULES)
    assert card_store.save(conn, a) == "updated"
    assert card_store.load(conn, d.id, 1).approval_valid
    different = CriteriaCard.model_validate(d.model_dump() | {"title": "a different title"})
    with pytest.raises(StoreError, match="already stored with different content; versions are permanent"):
        card_store.save(conn, different)


def test_every_version_is_kept(conn):
    a = approve(draft(), "A", RULES)
    v2 = approve(edit_criterion(a, "C3", "B", RULES, op=">", value=29), "B", RULES)
    card_store.save(conn, a)
    card_store.save(conn, v2)
    assert [(e.card_id, e.version, e.status) for e in card_store.entries(conn)] == [
        ("REQ-41-PRECONDITION", 1, "approved"), ("REQ-41-PRECONDITION", 2, "approved")]
    assert card_store.load(conn, a.id, 1).criteria[2].op == ">="


def test_card_citing_an_unknown_rule_is_refused(tmp_path):
    empty = db.connect(tmp_path / "empty.db")
    with pytest.raises(StoreError, match="not in the database"):
        card_store.save(empty, draft())


def test_content_cards_are_stored_too(conn):
    c = content_review.approve(dbvo.content_card(dbvo.load()), "A", accept_placeholders=True)
    assert card_store.save(conn, c) == "stored"
    assert card_store.load(conn, "REQ-41-CONTENT", 1) == c
    assert [e.kind for e in card_store.entries(conn)] == ["content"] and not card_store.entries(conn)[0].stale


# --- out of date ---------------------------------------------------------------------------------------------

def test_up_to_date_until_a_rule_changes(conn):
    a = approve(draft(), "A", RULES)
    card_store.save(conn, a)
    assert card_store.out_of_date(conn, a) == []
    change_rule(conn, "R-3.12-003", quote="Die Zeiten müssen mindestens 31 Tage umfassen.")
    assert card_store.out_of_date(conn, a) == ["R-3.12-003 (changed)"]


def test_a_rule_that_disappears(conn):
    a = approve(draft(), "A", RULES)
    card_store.save(conn, a)
    with conn:
        conn.execute("DELETE FROM requirements WHERE id = 'R-3.12-005'")
    assert card_store.out_of_date(conn, a) == ["R-3.12-005 (gone)"]


def test_votes_and_flags_do_not_make_a_card_out_of_date(conn):
    """Only the rule's words and labels matter, not the extractor's vote count or warnings."""
    a = approve(draft(), "A", RULES)
    card_store.save(conn, a)
    change_rule(conn, "R-3.12-003", votes=1, flags=["new warning"])
    assert card_store.out_of_date(conn, a) == []


def test_unstored_or_different_card(conn):
    a = approve(draft(), "A", RULES)
    with pytest.raises(StoreError, match="not stored"):
        card_store.out_of_date(conn, a)
    card_store.save(conn, a)
    with pytest.raises(StoreError, match="differs from the stored version"):
        card_store.out_of_date(conn, a.model_copy(update={"title": "changed in the file"}))


# --- export ---------------------------------------------------------------------------------------------------

def test_export_writes_the_newest_approved_version(conn, tmp_path):
    a = approve(draft(), "A", RULES)
    v2 = approve(edit_criterion(a, "C3", "B", RULES, op=">", value=29), "B", RULES)
    for c in (a, v2, edit_criterion(v2, "C3", "B", RULES, op=">=", value=30)):  # v3 is a draft
        card_store.save(conn, c)
    written, skipped = card_store.export(conn, tmp_path / "cards")
    assert [p.name for p in written] == ["REQ-41-PRECONDITION-v2.json"] and skipped == []
    assert CriteriaCard.model_validate_json(written[0].read_text(encoding="utf-8")) == v2


def test_export_skips_out_of_date_cards(conn, tmp_path):
    card_store.save(conn, approve(draft(), "A", RULES))
    change_rule(conn, "R-3.12-001", statement="changed by a new extraction")
    written, skipped = card_store.export(conn, tmp_path / "cards")
    assert written == [] and skipped == ["REQ-41-PRECONDITION v1: out of date (R-3.12-001 (changed))"]


def test_drafts_and_rejected_cards_are_not_exported(conn, tmp_path):
    from specpilot.review import reject
    card_store.save(conn, reject(draft(), "A", "no"))
    assert card_store.export(conn, tmp_path / "cards") == ([], [])


# --- the branch maker's check -------------------------------------------------------------------------------------

def test_branch_maker_refuses_unstored_and_out_of_date_cards(conn, tmp_path, capsys):
    dbfile = str(tmp_path / "team.db")
    a = approve(draft(), "A", RULES)
    save_card(a, tmp_path / "card.json")
    args = [str(tmp_path / "card.json"), "--db", dbfile, "--out", str(tmp_path / "b"), "--limit", "0"]
    assert branch_maker.main(args) == 1 and "store it first" in capsys.readouterr().err
    card_store.save(conn, a)
    assert branch_maker.main(args) == 0
    change_rule(conn, "R-3.12-004", quote="Für privat Versicherte ist die Anfrage nicht möglich.")
    assert branch_maker.main(args) == 1
    assert "out of date: rule(s) R-3.12-004 (changed)" in capsys.readouterr().err


def test_branch_maker_without_db_works_as_before(tmp_path):
    save_card(approve(draft(), "A", RULES), tmp_path / "card.json")
    assert branch_maker.main([str(tmp_path / "card.json"), "--out", str(tmp_path / "b"), "--limit", "0"]) == 0


# --- command line -----------------------------------------------------------------------------------------------

def test_cli(conn, tmp_path, capsys):
    dbfile = str(tmp_path / "team.db")
    save_card(approve(draft(), "A", RULES), tmp_path / "card.json")
    assert card_store.main(["save", str(tmp_path / "card.json"), "--db", dbfile]) == 0
    assert "REQ-41-PRECONDITION v1 (approved): stored" in capsys.readouterr().out
    assert card_store.main(["check", "--db", dbfile]) == 0
    assert "all approved cards are up to date" in capsys.readouterr().out
    change_rule(conn, "R-3.12-002", quote="Innerhalb der letzten 5 Monate muss eine Vorerkrankung liegen.")
    assert card_store.main(["check", "--db", dbfile]) == 1
    assert "OUT OF DATE: R-3.12-002 (changed)" in capsys.readouterr().out
    assert card_store.main(["export", "--db", dbfile, "--out", str(tmp_path / "cards")]) == 0
    assert "not exported" in capsys.readouterr().out
    assert card_store.main(["save", "--db", dbfile]) == 1


def test_cli_stores_content_cards(conn, tmp_path, capsys):
    c = content_review.approve(dbvo.content_card(dbvo.load()), "A", accept_placeholders=True)
    dbvo.save_card(c, tmp_path / "content.json")
    assert card_store.main(["save", str(tmp_path / "content.json"), "--db", str(tmp_path / "team.db")]) == 0
    assert "REQ-41-CONTENT v1 (approved): stored" in capsys.readouterr().out
