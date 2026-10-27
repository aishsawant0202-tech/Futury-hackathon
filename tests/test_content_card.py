"""Content card: approvable like a criteria card, locked to its content, placeholders accepted only on purpose."""
import datetime as dt

import pytest
from pydantic import ValidationError

from specpilot import content_review, dbvo, review
from specpilot.criteria import CardStatus
from specpilot.dbvo import ContentCard, content_card
from specpilot.review import ReviewError

NOW = dt.datetime(2027, 1, 5, 9, 0)


def draft() -> ContentCard:
    return content_card(dbvo.load())


def approved() -> ContentCard:
    return content_review.approve(draft(), "Examiner A", "ok", accept_placeholders=True, now=NOW)


# --- the card ----------------------------------------------------------------------------------------------

def test_draft_from_the_inventory():
    c = draft()
    assert c.status is CardStatus.DRAFT and c.version == 1 and not c.approval_valid
    assert c.placeholders() == 14 and len(c.rules) == 6


def test_round_trip_keeps_the_fingerprint(tmp_path):
    c = approved()
    dbvo.save_card(c, tmp_path / "c.json")
    again = dbvo.load_card(tmp_path / "c.json")
    assert again == c and again.content_hash() == c.content_hash() and again.approval_valid


def test_review_fields_do_not_change_the_fingerprint():
    assert approved().content_hash() == draft().content_hash()


# --- approve -------------------------------------------------------------------------------------------------

def test_placeholders_block_approval_unless_accepted():
    with pytest.raises(ReviewError, match="14 source.*placeholders"):
        content_review.approve(draft(), "Examiner A")
    c = approved()
    assert c.approval_valid and c.approved_with_placeholders == 14
    assert c.review_note == "ok | approved accepting 14 placeholder source(s)"


def test_approval_without_placeholders_needs_no_acceptance():
    data = draft().model_dump()
    for x in data["fields"] + data["rules"]:
        for s in x["sources"]:
            s |= {"placeholder": False, "quote": "Echtes Zitat aus der Spezifikation"}
    c = content_review.approve(ContentCard.model_validate(data), "Examiner A", now=NOW)
    assert c.approval_valid and c.approved_with_placeholders == 0 and "placeholder" not in c.review_note


def test_approve_needs_a_name_and_a_draft():
    with pytest.raises(ReviewError, match="reviewer's name"):
        content_review.approve(draft(), " ", accept_placeholders=True)
    with pytest.raises(ReviewError, match="only a draft"):
        content_review.approve(approved(), "A", accept_placeholders=True)


# --- approval is locked to the content --------------------------------------------------------------------------

def test_changed_card_loses_its_approval():
    c = approved()
    data = c.model_dump()
    next(r for r in data["rules"] if r["key"] == "gap_months")["values"]["p.135"] = 7
    changed = c.model_copy(update={"rules": ContentCard.model_validate(
        data | {"status": "draft", "reviewer": "", "reviewed_at": None, "review_note": "", "approved_hash": "",
                "approved_with_placeholders": 0}).rules})
    assert not changed.approval_valid
    with pytest.raises(ValidationError, match="changed after it was approved"):
        ContentCard.model_validate(changed.model_dump())


def test_placeholder_count_must_match_the_approval():
    data = approved().model_dump() | {"approved_with_placeholders": 3}
    with pytest.raises(ValidationError, match="accepted placeholder sources does not match"):
        ContentCard.model_validate(data)


def test_draft_cannot_carry_an_approval():
    with pytest.raises(ValidationError, match="cannot carry an approval"):
        ContentCard.model_validate(draft().model_dump() | {"approved_hash": "x" * 64})


# --- reject and edit -------------------------------------------------------------------------------------------------

def test_reject():
    r = content_review.reject(draft(), "Examiner A", "gap rule wrong", now=NOW)
    assert r.status is CardStatus.REJECTED and r.review_note == "gap rule wrong"
    with pytest.raises(ReviewError, match="a reason"):
        content_review.reject(draft(), "A", "")


def test_edit_makes_a_new_draft_and_voids_the_approval():
    e = content_review.edit_rule(approved(), "lookback_months", "p.136", 24, "Examiner B")
    assert e.version == 2 and e.status is CardStatus.DRAFT and not e.approval_valid
    assert e.rule("lookback_months", "p.136") == 24 and e.rule("lookback_months", "p.135") is None
    assert e.flags[-1] == "review: Examiner B changed lookback_months for p.136: 12 -> 24"


@pytest.mark.parametrize("key, reading, value, message", [
    ("gap_months", "p.135", 0, "not allowed"),
    ("gap_months", "p.135", "six", "not allowed"),
    ("order", "p.136", "sideways", "not allowed"),
    ("colour", "p.135", 1, "no content rule"),
    ("gap_months", "p.999", 6, "unknown reading"),
])
def test_invalid_edits_are_refused(key, reading, value, message):
    c = draft()
    with pytest.raises(ReviewError, match=message):
        content_review.edit_rule(c, key, reading, value, "A")
    assert c == draft()


def test_rejected_card_cannot_be_edited():
    with pytest.raises(ReviewError, match="rejected"):
        content_review.edit_rule(content_review.reject(draft(), "A", "no"), "gap_months", "p.135", 6, "A")


# --- the sheet and the terminal ----------------------------------------------------------------------------------------

def test_sheet_shows_values_sources_and_contradictions():
    text = content_review.sheet(draft())
    assert "lookback_months        p.135: null, p.136: 12   ⚑ the readings differ" in text
    assert "source p.136: „[PLACEHOLDER p.136] jedoch maximal innerhalb der letzten 12 Monate“" in text
    assert "⚑ 14 source(s) are PLACEHOLDERS" in text and "BEGINN-AU-n" in text


def scripted(*answers):
    it = iter(answers)
    out: list[str] = []
    return (lambda prompt: next(it)), out.append, out


def test_terminal_refuses_without_yes_then_approves_with_it():
    ask, say, out = scripted("a", "no", "a", "﻿YES", "looked at it", "q")
    c = content_review.run(draft(), "Examiner A", ask, say)
    assert c.approval_valid and c.approved_with_placeholders == 14


def test_terminal_edit_and_bad_values():
    ask, say, out = scripted("e", "gap_months", "p.135", "seven", "e", "gap_months", "p.135", "7", "!")
    assert content_review.run(draft(), "A", ask, say) is None
    assert any("a value must be true, false, a number" in o for o in out)
    assert any("p.135: 7, p.136: 6" in o for o in out)


def test_review_command_recognises_a_content_card(tmp_path, monkeypatch, capsys):
    dbvo.save_card(draft(), tmp_path / "content.json")
    answers = iter(["a", "YES", "", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert review.main([str(tmp_path / "content.json"), "--reviewer", "Examiner A"]) == 0
    assert "Saved REQ-41-CONTENT v1 (approved)" in capsys.readouterr().out
    assert dbvo.load_card(tmp_path / "content.json").approval_valid


def test_dbvo_command_saves_a_draft_card(tmp_path, capsys):
    assert dbvo.main(["--card-out", str(tmp_path / "c.json")]) == 0
    assert dbvo.load_card(tmp_path / "c.json") == draft()
    assert "python -m specpilot.review" in capsys.readouterr().out
