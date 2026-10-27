"""Hand-over checks between the pipeline steps: each passes clean output and catches what it is there for."""
import dataclasses

import pytest

from specpilot import card_store, checks, db
from specpilot.agents import verification
from specpilot.checks import AUTO_REVIEWER
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.review import approve
from specpilot.tools import rule_picker
from specpilot.tools.rule_picker import Exclusion
from tests.test_review import RULES, draft
from tests.test_rule_picker import GOLD_RULES, NOISE, rule

DOC = "Test-Spezifikation"
CARD = "REQ-41-PRECONDITION"
STEM = "REQ-41-PRECONDITION-v1-seed4711"


def messages(problems, severity=None) -> list[str]:
    return [p.message for p in problems if severity in (None, p.severity)]


# --- after_ingest ---------------------------------------------------------------------------------------------------

BODY = "Die Vorerkrankungsanfrage darf nur dann ausgelöst werden, wenn eine Bescheinigung vorliegt."


def raw(n: int, body: str = BODY) -> str:
    """A PDF page as pypdf reads it: the running header, then the body."""
    return f"Datenbaustein DBVO\nStand: 10.03.2026     Seite {n} von 4       Version 13.4\n{body}"


@pytest.fixture
def spec(tmp_path):
    c = db.connect(tmp_path / "spec.db")
    for n in range(1, 5):
        db.save_page(c, DOC, n, BODY)
    db.save_sections(c, DOC, [{"number": "3.12", "title": "Datenbaustein DBVO", "first_page": 3, "last_page": 4}])
    yield c
    c.close()


def test_ingest_clean(spec):
    assert checks.after_ingest(spec, DOC, ("3.12",), [raw(n) for n in range(1, 5)]) == []


def test_blank_page_is_a_warning_unreadable_page_an_error(spec):
    db.save_page(spec, DOC, 1, "")
    db.save_page(spec, DOC, 2, "")
    pages = [raw(1, ""), "", raw(3), raw(4)]  # p.1 has its header, p.2 has nothing at all
    problems = checks.after_ingest(spec, DOC, ("3.12",), pages)
    assert messages(problems, "warning") == ["blank pages (only the running header): p. 1"]
    assert messages(problems, "error") == ["no text could be read from p. 2 (a scanned image?)"]


def test_empty_page_inside_a_requested_section_is_an_error(spec):
    db.save_page(spec, DOC, 4, "")
    problems = checks.after_ingest(spec, DOC, ("3.12",), [raw(1), raw(2), raw(3), raw(4, "")])
    assert messages(problems) == ["p.4 in § 3.12 has no text: rules on it cannot be extracted"]


def test_lost_and_doubled_pages(spec):
    pages = [raw(1), raw(2), raw(2), raw(4), raw(5)]  # two PDF pages claim to be p.2; p.5 never reached the database
    problems = messages(checks.after_ingest(spec, DOC, ("3.12",), pages), "error")
    assert problems == ["PDF pages read as the same printed page number, one overwrote the other: 2",
                        "PDF pages not in the database: 5", "the PDF has 5 pages, the database 4"]


def test_unknown_section_and_empty_document(spec, tmp_path):
    assert messages(checks.after_ingest(spec, DOC, ("3.12", "9.9"), [raw(n) for n in range(1, 5)])) == [
        "§ 9.9 was not found among the headings; its rules cannot be extracted"]
    assert messages(checks.after_ingest(spec, "Other", ("3.12",), [])) == ["no pages of Other in the database"]


# --- after_extract --------------------------------------------------------------------------------------------------

P1 = """3.12 Datenbaustein DBVO – Vorerkrankungszeiten
Die Anfrage setzt voraus, dass für die aktuelle Arbeitsunfähigkeit eine Bescheinigung vorliegt.
Die Vorerkrankungszeiten müssen zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen."""
Q1 = "Die Anfrage setzt voraus, dass für die aktuelle Arbeitsunfähigkeit eine Bescheinigung vorliegt."
Q2 = "Die Vorerkrankungszeiten müssen zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen."


@pytest.fixture
def extracted(tmp_path):
    c = db.connect(tmp_path / "rules.db")
    db.save_page(c, DOC, 135, P1)
    db.save_sections(c, DOC, [{"number": "3.12", "title": "Datenbaustein DBVO", "first_page": 135, "last_page": 135}])
    for r in (rule(1, "sick note", quote=Q1), rule(2, "30 days", quote=Q2)):
        db.save_requirement(c, r)
    yield c
    c.close()


def test_extract_clean(extracted):
    assert checks.after_extract(extracted, ("3.12",), "41", DOC) == []


def test_section_without_rules(extracted):
    db.save_sections(extracted, DOC, [{"number": "3.12", "title": "DBVO", "first_page": 135, "last_page": 135},
                                      {"number": "3.13", "title": "DBHE", "first_page": 135, "last_page": 135}])
    assert "§ 3.13: the extractor found no rules" in messages(checks.after_extract(extracted, ("3.12", "3.13"), "41", DOC))


def test_too_few_verified_quotes_and_nothing_testable(extracted):
    for n, q in ((1, Q1), (2, Q2)):
        db.save_requirement(extracted, rule(n, "x", quote=q, verified=False))
    problems = messages(checks.after_extract(extracted, ("3.12",), "41", DOC), "error")
    assert problems == ["§ 3.12: only 0 of 2 quotes were found on their page (at least 80% needed); "
                        "the extraction is not reliable",
                        "no employer rule with a verified quote for reason 41: nothing to test"]


def test_uncovered_sentences_and_contradictions_are_warnings(extracted):
    db.delete_section(extracted, "3.12")
    db.save_requirement(extracted, rule(1, "sick note", quote=Q1, flags=["Contradiction with R-3.12-009: ..."]))
    problems = checks.after_extract(extracted, ("3.12",), "41", DOC)
    assert checks.errors(problems) == []
    assert messages(problems) == ["§ 3.12: 1 of 2 sentences are not covered by any rule; some may be rules the "
                                  "extractor missed",
                                  "1 rule(s) flagged as contradicting another rule: R-3.12-001"]


# --- stale cards, rule picker, translator ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "team.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def test_card_on_changed_rules_must_be_translated_again(conn):
    card = approve(draft(), "x", RULES)
    card_store.save(conn, card)
    assert checks.stale_cards(conn) == []
    db.save_requirement(conn, db.get_requirement(conn, "R-3.12-003").model_copy(update={"quote": "neuer Text, 31 Tage"}))
    assert messages(checks.stale_cards(conn)) == [
        f"{CARD} v1 (approved) rests on rules that changed since: R-3.12-003 (changed); it must be translated again"]
    assert "out of date" in messages(checks.before_branches(conn, card))[0]


def test_pick_accounts_for_every_rule(conn):
    p = rule_picker.pick(conn, CARD)
    assert checks.errors(checks.after_pick(conn, p)) == []
    assert messages(checks.after_pick(conn, p), "warning") == [
        "picked rules found in only one extraction reading: R-3.12-005"]


def test_pick_that_loses_or_hides_a_rule(conn):
    p = rule_picker.pick(conn, CARD)
    lost = dataclasses.replace(p, excluded=p.excluded[1:])
    assert messages(checks.after_pick(conn, lost), "error") == [
        f"rules about reason 41 neither picked nor left out: {p.excluded[0].rule.id}"]
    silent = dataclasses.replace(p, excluded=[Exclusion(p.excluded[0].rule, [])] + p.excluded[1:])
    assert messages(checks.after_pick(conn, silent), "error") == [
        f"rules left out without a reason: {p.excluded[0].rule.id}"]


def test_translate_clean_and_with_a_rule_lost(conn):
    p = rule_picker.pick(conn, CARD)
    card = draft()
    assert checks.errors(checks.after_translate(card, p)) == []
    extra = rule(20, "a rule picked after the card was made", quote="Ein weiterer Satz aus der Spezifikation hier.")
    more = dataclasses.replace(p, picked=p.picked + [extra])
    assert messages(checks.after_translate(card, more), "error") == ["picked rules missing from the card: R-3.12-020"]


# --- auto approval, before_branches -----------------------------------------------------------------------------------

def test_auto_approval_of_a_clean_card(conn):
    card, problems = checks.auto_approve(draft(), RULES)
    assert problems == [] and card.status is CardStatus.APPROVED and card.reviewer == AUTO_REVIEWER
    assert card.approval_valid
    card_store.save(conn, card)
    assert checks.before_branches(conn, card) == []


def test_auto_approval_refuses_when_the_cross_check_disagrees():
    flagged = CriteriaCard.model_validate(draft().model_dump() | {"flags": [
        "Cross-check (rules in reverse order) disagrees on C3 total_days_12m >= 30: wrong"]})
    card, problems = checks.auto_approve(flagged, RULES)
    assert card is None and messages(problems) == [
        "Cross-check (rules in reverse order) disagrees on C3 total_days_12m >= 30: wrong"]


def test_auto_approval_refuses_unknown_rules_and_non_drafts():
    card, problems = checks.auto_approve(draft(), RULES[:-1])
    assert card is None and messages(problems) == [
        "rules not in the database, the card cannot be checked: R-3.12-007"]
    approved = approve(draft(), "x", RULES)
    assert checks.auto_approve(approved, RULES)[0] is None


def test_branches_need_a_stored_approved_card(conn):
    d = draft()
    assert messages(checks.before_branches(conn, d)) == [f"{CARD} v1 is not approved, or changed after approval"]
    card = approve(d, "x", RULES)
    assert "is not stored" in messages(checks.before_branches(conn, card))[0]


# --- verification ------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    from scripts.make_verification_scenarios import make
    root = tmp_path_factory.mktemp("scenarios")
    return make(root / "out", root / "vendor")


def test_before_verify(scenarios, tmp_path):
    assert checks.before_verify([scenarios["certified"]], scenarios["expected"], STEM) == []
    problems = checks.before_verify([], scenarios["expected"], "REQ-41-OTHER-v2-seed1")
    assert messages(problems, "error") == ["no vendor report given",
                                           f"no expected results for REQ-41-OTHER-v2-seed1 in {scenarios['expected']}"]
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    assert "cannot be read as a PDF" in messages(checks.before_verify([tmp_path / "bad.pdf"], scenarios["expected"], STEM),
                                                 "warning")[0]


def test_missing_cases_are_counted(scenarios):
    v = verification.verify(scenarios["missing_information"], scenarios["expected"])
    assert v.missing_cases == ["DUMMY-0004", "DUMMY-0014", "DUMMY-0024", "DUMMY-0034"]
    assert verification.verify(scenarios["certified"], scenarios["expected"]).missing_cases == []
    undercounted = dataclasses.replace(v, missing_cases=v.missing_cases[:1])
    assert messages(checks.after_verify(undercounted, scenarios["missing_information"], scenarios["expected"], STEM)) == [
        f"{v.report}: 1 case(s) counted as missing information, recomputed 4"]


@pytest.mark.parametrize("name", ["certified", "rejected", "missing_information"])
def test_after_verify_agrees_with_all_three_verdicts(scenarios, name):
    v = verification.verify(scenarios[name], scenarios["expected"])
    assert checks.after_verify(v, scenarios[name], scenarios["expected"], STEM) == []


def test_after_verify_catches_a_wrong_verdict_and_a_hidden_gap(scenarios):
    path = scenarios["missing_information"]
    v = verification.verify(path, scenarios["expected"])
    tampered = dataclasses.replace(v, verdict=verification.CERTIFIED, missing_information=v.missing_information[1:])
    problems = messages(checks.after_verify(tampered, path, scenarios["expected"], STEM), "error")
    assert problems == [f"{v.report}: cases not returned by the vendor and not flagged: DUMMY-0004",
                        f"{v.report}: verdict CERTIFIED, the flags say MISSING INFORMATION"]


def test_after_verify_catches_a_report_on_another_dummy_file(scenarios):
    v = verification.verify(scenarios["certified"], scenarios["expected"])
    problems = messages(checks.after_verify(v, scenarios["certified"], scenarios["expected"], "REQ-41-PRECONDITION-v2-seed1"))
    assert problems == [f"{v.report}: the vendor tested {STEM}, this job made REQ-41-PRECONDITION-v2-seed1; "
                        f"the report was compared with another job's answers"]


def test_after_verify_catches_miscounted_matches(scenarios):
    v = verification.verify(scenarios["rejected"], scenarios["expected"])
    problems = messages(checks.after_verify(dataclasses.replace(v, matches=v.matches + 1), scenarios["rejected"],
                                            scenarios["expected"], STEM))
    assert problems == [f"{v.report}: 33 matches + 2 cases with differences != 34 compared"]
