"""Review: approve / reject / edit / remove / pick a reading, with every safety rule, plus the terminal loop."""
from datetime import datetime

import pytest

from specpilot import db, review
from specpilot.agents.translator import CardDraft, build_card
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.review import ReviewError, approve, choose_reading, edit_criterion, reject, remove_criterion
from specpilot.tools import card_guards
from specpilot.tools.rule_picker import CARDS, Pick
from tests.test_card_guards import with_c
from tests.test_rule_picker import GOLD_RULES
from tests.test_translator import good_answer

RULES = GOLD_RULES
NOW = datetime(2026, 10, 27, 14, 30)


def draft(answer: dict | None = None) -> CriteriaCard:
    """A draft card as the translator would hand it over: pick warnings + guard flags."""
    card = build_card(Pick(CARDS["REQ-41-PRECONDITION"], RULES), CardDraft.model_validate(answer or good_answer()), "test-model")
    found = [g.text() for g in card_guards.check(card, RULES)]
    return CriteriaCard.model_validate(card.model_dump() | {"flags": card.flags + found})


ALT = {"fact": "total_days_12m", "op": ">", "value": 30, "source_rule_ids": ["R-3.12-003"],
       "quote": "mindestens 30 Tage", "label": "strict reading"}


def ambiguous() -> CriteriaCard:
    return draft(with_c(3, label="main reading", ambiguity="test: two readings", alternatives=[ALT]))


# --- the sheet --------------------------------------------------------------------------------

def test_sheet_shows_plain_sentences_quotes_and_rules():
    text = review.review_sheet(draft(), RULES)
    assert "C3  total_days_12m >= 30" in text
    assert "must be at least 30 days" in text
    assert "must be NO" in text  # C5 marginal_employment = false
    assert "R-3.12-003 p.135: „Die Vorerkrankungszeiten müssen" in text
    assert "R-3.12-006: Contradiction" in text


def test_sheet_puts_guard_flags_under_their_criterion():
    text = review.review_sheet(draft(with_c(3, op=">")), RULES)
    c3, c4 = text.index("  C3  "), text.index("  C4  ")
    assert c3 < text.index("[limit] C3") < c4


def test_plain_sentences():
    c = draft().criteria
    assert review.plain(c[0]).endswith("-> must be YES")
    assert review.plain(c[2]).endswith("-> must be at least 30 days")
    between = draft(with_c(3, op="between", value=[30, 60])).criteria[2]
    assert review.plain(between).endswith("between 30 and 60 days (both included)")


# --- approve ----------------------------------------------------------------------------------

def test_approve_locks_the_content():
    a = approve(draft(), "Examiner A", RULES, "looks right", now=NOW)
    assert a.status is CardStatus.APPROVED and a.approval_valid
    assert (a.reviewer, a.reviewed_at, a.review_note) == ("Examiner A", NOW, "looks right")
    assert a.approved_hash == draft().content_hash()


def test_approve_needs_a_name_and_a_draft():
    with pytest.raises(ReviewError, match="reviewer's name"):
        approve(draft(), "  ", RULES)
    a = approve(draft(), "A", RULES)
    with pytest.raises(ReviewError, match="only a draft"):
        approve(a, "A", RULES)
    with pytest.raises(ReviewError, match="only a draft"):
        approve(reject(draft(), "A", "wrong"), "A", RULES)


def test_guard_errors_block_approval_unless_accepted():
    card = draft(with_c(3, op=">"))
    with pytest.raises(ReviewError, match=r"(?s)guard errors.*\[limit\] C3"):
        approve(card, "A", RULES)
    a = approve(card, "A", RULES, "checked by hand", accept_guard_errors=True)
    assert a.approval_valid and a.review_note == "checked by hand | approved accepting 1 guard error(s)"


def test_warnings_do_not_block_approval():
    card = draft(with_c(5, fact="current_open_end"))  # fact_terms warning only
    assert any(f.startswith("[fact_terms]") for f in card.flags)
    assert approve(card, "A", RULES).approval_valid


def test_missing_facts_block_approval():
    card = draft(good_answer(unused_rules=[{"rule_id": "R-3.12-005", "reason": "counting"}, {"rule_id": "R-3.12-006", "reason": "x" * 5}],
                             missing_facts=[{"rule_ids": ["R-3.12-007"], "quote": "12 Monate", "needed": "months reported back"}]))
    with pytest.raises(ReviewError, match="missing facts"):
        approve(card, "A", RULES)


def test_placeholder_ids_block_approval():
    from specpilot.tools import gold
    with pytest.raises(ReviewError, match="placeholder rule ids"):
        approve(gold.load("REQ-41-PRECONDITION"), "A", RULES)


# --- reject ----------------------------------------------------------------------------------------

def test_reject():
    r = reject(draft(), "Examiner A", "C3 should cover 12 months only", now=NOW)
    assert r.status is CardStatus.REJECTED and r.review_note == "C3 should cover 12 months only"
    with pytest.raises(ReviewError, match="a reason"):
        reject(draft(), "A", " ")


# --- edit ----------------------------------------------------------------------------------------------

def test_edit_makes_a_new_draft_version_and_rechecks():
    card = draft(with_c(3, op=">"))  # wrong operator, guard error
    fixed = edit_criterion(card, "C3", "Examiner A", RULES, op=">=")
    assert fixed.version == 2 and fixed.status is CardStatus.DRAFT
    assert fixed.criteria[2].op == ">="
    assert not review.guard_errors(fixed)  # guards re-ran, the old error is gone
    assert "review: Examiner A edited C3: total_days_12m > 30 -> total_days_12m >= 30" in fixed.flags


def test_edit_can_introduce_a_guard_error():
    worse = edit_criterion(draft(), "C3", "A", RULES, value=31)
    assert any(f.startswith("[limit] C3") for f in worse.flags)


def test_editing_an_approved_card_voids_the_approval():
    a = approve(draft(), "A", RULES)
    e = edit_criterion(a, "C3", "B", RULES, value=29, op=">")
    assert e.status is CardStatus.DRAFT and e.version == 2 and not e.approval_valid
    assert e.reviewer == "" and e.approved_hash == ""


def test_invalid_edits_are_refused_and_change_nothing():
    card = draft()
    with pytest.raises(ReviewError, match="must be a whole number"):
        edit_criterion(card, "C3", "A", RULES, value="thirty")
    with pytest.raises(ReviewError, match="not allowed for current_certified"):
        edit_criterion(card, "C1", "A", RULES, op=">")
    with pytest.raises(ReviewError, match="no criterion C9"):
        edit_criterion(card, "C9", "A", RULES, value=1)
    with pytest.raises(ReviewError, match="edit one or more"):
        edit_criterion(card, "C3", "A", RULES, source_rule_ids=["R-3.12-001"])
    with pytest.raises(ReviewError, match="run with --db"):
        edit_criterion(card, "C3", "A", None, value=30)
    assert card == draft()


def test_rejected_cards_cannot_be_edited():
    with pytest.raises(ReviewError, match="rejected"):
        edit_criterion(reject(draft(), "A", "no"), "C3", "A", RULES, value=30)


# --- remove ---------------------------------------------------------------------------------------------

def test_remove_moves_the_rule_to_unused_and_renumbers():
    card = remove_criterion(draft(), "C1", "Examiner A", "covered elsewhere", RULES)
    assert [c.id for c in card.criteria] == ["C1", "C2", "C3", "C4"]
    assert card.criteria[0].fact == "prior_certified_6m"
    assert any(u.rule_id == "R-3.12-001" and "covered elsewhere" in u.reason for u in card.unused_rules)
    assert card.version == 2


def test_remove_keeps_a_rule_another_criterion_still_cites():
    card = remove_criterion(draft(), "C4", "A", "test", RULES)  # R-3.12-004 is still cited by the Minijob criterion
    assert not any(u.rule_id == "R-3.12-004" for u in card.unused_rules)


def test_remove_needs_a_reason_and_keeps_one_criterion():
    with pytest.raises(ReviewError, match="needs a reason"):
        remove_criterion(draft(), "C1", "A", "", RULES)
    card = draft()
    for _ in range(4):
        card = remove_criterion(card, "C1", "A", "test", RULES)
    with pytest.raises(ReviewError, match="at least one criterion"):
        remove_criterion(card, "C1", "A", "test", RULES)


# --- pick a reading ---------------------------------------------------------------------------------------

def test_choose_reading():
    card = choose_reading(ambiguous(), "C3", "strict reading", "Examiner A", RULES)
    c3 = card.criteria[2]
    assert (c3.op, c3.value, c3.alternatives, c3.ambiguity) == (">", 30, [], "")
    assert any("chose reading 'strict reading' for C3" in f for f in card.flags)
    assert any(f.startswith("[limit] C3") for f in card.flags)  # the guards say the quote reads >= 30


def test_choose_main_reading_and_bad_labels():
    assert choose_reading(ambiguous(), "C3", "main reading", "A", RULES).criteria[2].op == ">="
    with pytest.raises(ReviewError, match="no reading"):
        choose_reading(ambiguous(), "C3", "nope", "A", RULES)
    with pytest.raises(ReviewError, match="only one reading"):
        choose_reading(draft(), "C1", "x", "A", RULES)


# --- audit and files ---------------------------------------------------------------------------------------

def test_audit_and_file_round_trip(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    a = approve(draft(), "A", RULES)
    review.log_action(conn, a, "approved")
    entry = db.list_audit(conn, a.id)[-1]
    assert entry.actor == "examiner" and f"content_hash={a.content_hash()}" in entry.detail
    review.save_card(a, tmp_path / "card.json")
    assert review.load_card(tmp_path / "card.json") == a


# --- the terminal loop ---------------------------------------------------------------------------------------

def scripted(*answers):
    it = iter(answers)
    out: list[str] = []
    return (lambda prompt: next(it)), out.append, out


def test_terminal_approve():
    ask, say, out = scripted("a", "all good", "q")
    card = review.run(draft(), "Examiner A", RULES, None, ask, say)
    assert card.approval_valid and card.review_note == "all good"
    assert any("Approved REQ-41-PRECONDITION v1" in o for o in out)


def test_terminal_fix_then_approve():
    ask, say, out = scripted("a", "no", "e", "C3", "op", ">=", "a", "", "q")
    card = review.run(draft(with_c(3, op=">")), "A", RULES, None, ask, say)
    assert card.version == 2 and card.criteria[2].op == ">=" and card.approval_valid


def test_terminal_reports_refusals_and_can_discard():
    ask, say, out = scripted("e", "C3", "value", "thirty", "e", "C1", "op", ">", "x", "C1", "", "!")
    assert review.run(draft(), "A", RULES, None, ask, say) is None
    assert any("a value must be true, false" in o for o in out)
    assert any(o.startswith("Not done: edit refused") for o in out)
    assert any("needs a reason" in o for o in out)


def rules_db(tmp_path) -> str:
    conn = db.connect(tmp_path / "rules.db")
    for r in RULES:
        db.save_requirement(conn, r)
    conn.close()
    return str(tmp_path / "rules.db")


def test_cli_saves_the_file_and_logs(tmp_path, monkeypatch, capsys):
    path, dbfile = tmp_path / "card.json", rules_db(tmp_path)
    review.save_card(draft(), path)
    answers = iter(["a", "", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert review.main([str(path), "--reviewer", "Examiner A", "--db", dbfile]) == 0
    assert review.load_card(path).approval_valid
    assert "Saved REQ-41-PRECONDITION v1 (approved)" in capsys.readouterr().out
    assert db.list_audit(db.connect(dbfile), "REQ-41-PRECONDITION")[-1].action == "approved REQ-41-PRECONDITION v1"


def test_cli_quit_without_saving_leaves_the_file(tmp_path, monkeypatch):
    path = tmp_path / "card.json"
    review.save_card(draft(), path)
    before = path.read_bytes()
    answers = iter(["a", "", "!"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert review.main([str(path), "--reviewer", "A", "--db", rules_db(tmp_path)]) == 0
    assert path.read_bytes() == before


def test_cli_without_db_cannot_approve(tmp_path, monkeypatch, capsys):
    path = tmp_path / "card.json"
    review.save_card(draft(), path)
    answers = iter(["a", "", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    review.main([str(path), "--reviewer", "A"])
    out = capsys.readouterr().out
    assert "No --db given" in out and "Not done: approval needs the rule texts" in out
    assert review.load_card(path).status is CardStatus.DRAFT


# --- a card changed outside the tool ----------------------------------------------------------------------

def tampered() -> CriteriaCard:
    """A draft whose C3 was changed in the file after the translator stored its (clean) guard flags."""
    data = draft().model_dump()
    data["criteria"][2]["value"] = 31
    return CriteriaCard.model_validate(data)


def test_approval_rechecks_instead_of_trusting_stored_flags():
    card = tampered()
    assert not review.guard_errors(card)  # the stored flags know nothing about the change
    with pytest.raises(ReviewError, match=r"(?s)guard errors.*\[limit\] C3: card says total_days_12m >= 31"):
        approve(card, "A", RULES)


def test_terminal_shows_fresh_problems_when_opening_a_card():
    ask, say, out = scripted("!")
    review.run(tampered(), "A", RULES, None, ask, say)
    assert "[limit] C3: card says total_days_12m >= 31" in out[0]


def test_approval_needs_every_cited_rule():
    with pytest.raises(ReviewError, match="run with --db"):
        approve(draft(), "A", None)
    with pytest.raises(ReviewError, match=r"rules \['R-3.12-007'\] are not in the database"):
        approve(draft(), "A", RULES[:6])


def test_byte_order_mark_from_piped_input_is_ignored():
    """Windows PowerShell can put an invisible BOM before the first piped line."""
    ask, say, out = scripted("﻿a", "", "q")
    assert review.run(draft(), "A", RULES, None, ask, say).approval_valid
