"""Extraction agent tests with a fake model: no gateway, no key, same result every time."""
import pytest

from specpilot import db, llm
from specpilot.agents import extractor
from specpilot.agents.extractor import Conflict, Conflicts, RuleDraft, RuleDrafts, merge, windows
from specpilot.models import Actor, RuleKind

DOC = "Test-Spezifikation"
P135 = """3.12 Datenbaustein DBVO – Vorerkrankungszeiten
Die Vorerkrankungsanfrage darf nur dann ausgelöst werden, wenn
• dem Arbeitgeber eine Arbeitsunfähigkeitsbescheini-
gung vorliegt,
• die Zeiten mindestens 30 Tage umfassen.
Hierbei ist bis zur erstmalig bescheinigten Erkrankung zurückzugehen (hierfür gilt keine zeitliche Begrenzung)."""
P136 = """Hierbei ist bis zur erstmalig bescheinigten Erkrankung zurückzugehen (jedoch maximal innerhalb der letzten 12 Monate).
3.13 Datenbaustein DBHE – Höhe
Text von 3.13, gehört nicht zu 3.12."""


def _draft(quote, page=135, kind=RuleKind.PRECONDITION, actor=Actor.EMPLOYER, codes=("41",)):
    return RuleDraft(page=page, quote=quote, statement=quote[:40], kind=kind, actor=actor, reason_codes=list(codes))


SICK_NOTE = _draft("dem Arbeitgeber eine Arbeitsunfähigkeitsbescheinigung vorliegt")
DAYS_30 = _draft("die Zeiten mindestens 30 Tage umfassen")
NO_LIMIT = _draft("bis zur erstmalig bescheinigten Erkrankung zurückzugehen (hierfür gilt keine zeitliche Begrenzung)")
MAX_12 = _draft("bis zur erstmalig bescheinigten Erkrankung zurückzugehen (jedoch maximal innerhalb der letzten 12 Monate)", page=136, actor=Actor.INSURER, codes=("61",))
INVENTED = _draft("die Anfrage ist erst ab 42 Tagen zulässig")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.save_page(c, DOC, 92, "Beispielseite aus 3.7", "3.7")
    db.save_page(c, DOC, 135, P135, "3.12")
    db.save_page(c, DOC, 136, P136, "3.12")
    db.save_sections(c, DOC, [
        {"number": "3.7", "title": "DBEE", "first_page": 92, "last_page": 92},
        {"number": "3.12", "title": "DBVO", "first_page": 135, "last_page": 136},
        {"number": "3.13", "title": "DBHE", "first_page": 136, "last_page": 136},
    ])
    yield c
    c.close()


def test_section_text_is_cut_at_the_next_heading(conn):
    pages = extractor.section_text(conn, DOC, "3.12")
    assert [p for p, _ in pages] == [135, 136]
    assert pages[0][1].startswith("3.12 Datenbaustein")
    assert "3.13" not in pages[1][1] and "gehört nicht" not in pages[1][1]


def test_windows():
    pages = [(p, "") for p in (1, 2, 3)]
    assert [[p for p, _ in w] for w in windows(pages, 1)] == [[1], [2], [3]]
    assert [[p for p, _ in w] for w in windows(pages, 2)] == [[1, 2], [3]]
    assert [[p for p, _ in w] for w in windows(pages, 0)] == [[1, 2, 3]]


def test_merge_counts_runs_not_duplicates():
    longer = _draft("Die Vorerkrankungsanfrage darf nur dann ausgelöst werden, wenn dem Arbeitgeber eine Arbeitsunfähigkeitsbescheinigung vorliegt")
    runs = [
        [(SICK_NOTE, True), (DAYS_30, True)],
        [(longer, False), (SICK_NOTE, True)],  # same rule twice in one run = still one vote
        [(SICK_NOTE, True)],
    ]
    result = {d.quote: (votes, ok) for d, votes, ok in merge(runs)}
    assert result == {SICK_NOTE.quote: (3, True), DAYS_30.quote: (1, True)}


def test_extract_and_conflicts_with_fake_model(conn, monkeypatch):
    answers = {135: [SICK_NOTE, DAYS_30, NO_LIMIT, INVENTED], 136: [MAX_12]}
    seen_prompts = []

    def fake_ask_json(model, system, user, schema):
        seen_prompts.append(user)
        if schema is RuleDrafts:
            pages = [p for p in answers if f"[Seite {p}]" in user]
            return RuleDrafts(rules=[d for p in pages for d in answers[p]])
        assert schema is Conflicts
        no_limit = next(line.split()[0] for line in user.splitlines() if "keine zeitliche" in line)
        max_12 = next(line.split()[0] for line in user.splitlines() if "maximal innerhalb" in line)
        return Conflicts(conflicts=[Conflict(rule_ids=[no_limit, max_12, "R-9.9-999"], explanation="no limit vs 12 months")])

    monkeypatch.setattr(llm, "ask_json", fake_ask_json)
    rules = extractor.run(conn, ("3.12",), DOC)

    # 2 pages: window 1 (2 calls) + window 2 = whole section, so only 2 distinct runs
    assert sum(e.action.startswith("extract p.") for e in db.list_audit(conn, "3.12")) == 3
    assert [r.id for r in rules] == [f"R-3.12-{n:03d}" for n in range(1, 6)]
    by_quote = {r.quote: r for r in rules}

    assert by_quote[SICK_NOTE.quote].quote_verified and by_quote[SICK_NOTE.quote].votes == 2
    invented = by_quote[INVENTED.quote]
    assert not invented.quote_verified and invented.flags[0].startswith("Quote not found")
    for q in (NO_LIMIT.quote, MAX_12.quote):
        assert any(f.startswith("Contradiction with R-3.12-") for f in by_quote[q].flags)
    assert by_quote[MAX_12.quote].actor is Actor.INSURER

    # Saved as drafts, with an audit row per model call
    assert len(db.list_requirements(conn)) == 5
    assert any("contradiction check" in e.action for e in db.list_audit(conn, "3.12"))


def test_reason_code_label(conn, monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _draft("eine Regel mit erfundenem Code", codes=("45",))  # not in the 3.1.18 list (p.44)

    all_messages = _draft("die Zeiten mindestens 30 Tage umfassen", codes=())
    monkeypatch.setattr(
        llm, "ask_json",
        lambda m, s, u, schema: RuleDrafts(rules=[SICK_NOTE, MAX_12, all_messages]) if schema is RuleDrafts else Conflicts(conflicts=[]),
    )
    extractor.run(conn, ("3.12",), DOC)

    for_41 = {r.quote for r in db.list_requirements(conn, reason_code="41")}
    assert for_41 == {SICK_NOTE.quote, all_messages.quote}  # [] = applies to every message
    assert {r.quote for r in db.list_requirements(conn, reason_code="61")} == {MAX_12.quote, all_messages.quote}


def test_rules_without_code_get_their_sections_codes(conn, monkeypatch):
    db.save_page(conn, DOC, 136, "Erfolgt die Meldung im Zusammenhang mit Übergangsgeld (3.1.18 „Abgabegrund“ = „11“, „12“) ...\n" + P136, "3.12")
    assert extractor.section_reason_codes(conn, DOC, "3.12") == ["11", "12"]

    no_code = _draft("die Zeiten mindestens 30 Tage umfassen", codes=())
    monkeypatch.setattr(
        llm, "ask_json",
        lambda m, s, u, schema: RuleDrafts(rules=[SICK_NOTE, no_code]) if schema is RuleDrafts else Conflicts(conflicts=[]),
    )
    codes = {r.quote: r.reason_codes for r in extractor.run(conn, ("3.12",), DOC)}
    assert codes == {SICK_NOTE.quote: ["41"], no_code.quote: ["11", "12"]}  # the model's own code is kept


def test_general_section_stays_all_messages(conn):
    many = ", ".join(f"„{c}“" for c in ("01", "02", "03", "04", "11", "12", "21", "22", "23", "31", "41", "42"))
    db.save_page(conn, DOC, 135, "3.12 Datenbaustein\nDie Abgabegründe „Abgabegrund“ = " + many, "3.12")
    assert extractor.section_reason_codes(conn, DOC, "3.12") == []


def test_sentences_skip_layout_rows():
    text = "005-\n012\n008 n M EEL-ABAG Beginn der Entgeltersatzleistung Arbeitgeber\njhjjmmtt\nMaßgebend für die Meldung ist der Tag des Be-\nginns der Leistung. Kurz."
    assert extractor.sentences(text) == ["Maßgebend für die Meldung ist der Tag des Beginns der Leistung."]


def test_coverage_pass_recovers_a_missed_rule(conn, monkeypatch):
    def fake(model, system, user, schema):
        if schema is Conflicts:
            return Conflicts(conflicts=[])
        if "3.12 Datenbaustein" in user:  # a normal window (the coverage pass sends no headings): the 30-day rule is missed
            return RuleDrafts(rules=[SICK_NOTE])
        return RuleDrafts(rules=[DAYS_30] if "30 Tage" in user else [])  # coverage pass: only the uncovered sentences

    monkeypatch.setattr(llm, "ask_json", fake)
    quotes = {r.quote for r in extractor.run(conn, ("3.12",), DOC)}
    assert DAYS_30.quote in quotes
    coverage = [e for e in db.list_audit(conn, "3.12") if e.action.startswith("coverage:")]
    assert coverage and "mindestens 30 Tage" not in coverage[-1].detail


def test_conflict_groups_pick_candidates():
    from specpilot.models import Requirement

    def rule(n, quote):
        return Requirement(id=f"R-3.12-{n:03d}", section="3.12", page=135, quote=quote, statement="x", kind=RuleKind.CONTENT)

    a = rule(1, "Erkrankungen, bei welchen nicht mehr als 6 Monate vergangen sind")
    b = rule(2, "Erkrankungen, zwischen welchen mehr als 6 Monate Arbeitsfähigkeit bestand")
    c = rule(3, "Bei Meldung des Arbeitgebers ist hier nur die Grundstellung zulässig")
    groups = extractor.conflict_groups([a, b, c])
    assert [[r.id for r in g] for g in groups] == [["R-3.12-001", "R-3.12-002"]]  # shared limit "6 Monate"; c alone
    many = [rule(n, f"Regel {n}: innerhalb von 6 Monaten zu melden") for n in range(1, 26)]
    assert [len(g) for g in extractor.conflict_groups(many)] == [10, 10, 5]


def test_rerun_replaces_the_sections_rules(conn, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", lambda m, s, u, schema: RuleDrafts(rules=[SICK_NOTE, DAYS_30]) if schema is RuleDrafts else Conflicts(conflicts=[]))
    extractor.run(conn, ("3.12",), DOC)
    monkeypatch.setattr(llm, "ask_json", lambda m, s, u, schema: RuleDrafts(rules=[SICK_NOTE]) if schema is RuleDrafts else Conflicts(conflicts=[]))
    extractor.run(conn, ("3.12",), DOC)
    assert [r.quote for r in db.list_requirements(conn)] == [SICK_NOTE.quote]  # no stale rule from the first run
