"""Rule picker: picks the right extracted rules for a card and explains every rule it leaves out."""
import pytest

from specpilot import db
from specpilot.criteria import CriteriaCard
from specpilot.models import Actor, Requirement, RuleKind
from specpilot.tools import gold, rule_picker
from specpilot.tools.rule_picker import CardSpec, PickError, pick

E, I = Actor.EMPLOYER, Actor.INSURER
PRE, CONTENT = RuleKind.PRECONDITION, RuleKind.CONTENT


def rule(n, statement, page=135, kind=PRE, actor=E, codes=("41",), verified=True, votes=3, flags=(), section="3.12",
         quote=None):
    return Requirement(id=f"R-{section}-{n:03d}", section=section, page=page,
                       quote=quote or f"Testzitat Nummer {n} aus der Spezifikation", statement=statement, kind=kind,
                       actor=actor, reason_codes=list(codes), quote_verified=verified, votes=votes, flags=list(flags))


# What the extractor should give for § 3.12, mirroring the gold card's rules 901-907.
# Test fixtures: the German quotes are invented in the style of the spec, not copied from it.
GOLD_RULES = [
    rule(1, "The current absence must be certified by a sick note.",
         quote="Die Anfrage setzt voraus, dass für die aktuelle Arbeitsunfähigkeit eine Arbeitsunfähigkeitsbescheinigung vorliegt."),
    rule(2, "There must be a certified previous illness in the 6 months before.",
         quote="Innerhalb der letzten 6 Monate vor Beginn der Arbeitsunfähigkeit muss eine bescheinigte Vorerkrankung liegen."),
    rule(3, "Previous illnesses and the current one must cover at least 30 days.", votes=2,
         quote="Die Vorerkrankungszeiten müssen zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen."),
    rule(4, "Not possible for privately insured employees or Minijobs.",
         quote="Für privat krankenversicherte und geringfügig beschäftigte Arbeitnehmer ist die Anfrage nicht möglich."),
    rule(5, "An open-ended absence is counted up to the report date plus 7 days.", votes=1,
         quote="Ist das Ende der Arbeitsunfähigkeit offen, wird bis zum Meldedatum zuzüglich 7 Tage gezählt."),
    rule(6, "Go back while the gap is not more than 6 months.",
         flags=["Contradiction with R-3.12-007: 'nicht mehr als' vs 'nicht mindestens'"],
         quote="Es ist zurückzugehen, solange zwischen zwei Erkrankungen nicht mehr als 6 Monate liegen."),
    rule(7, "Go back at most 12 months.", page=136,
         flags=["Contradiction with R-3.12-006: no time limit vs 12 months"],
         quote="Zurückzugehen ist jedoch maximal innerhalb der letzten 12 Monate."),
]
# ... plus rules the picker must leave out
NOISE = [
    rule(8, "The insurer answers with reason 61.", page=137, actor=I, codes=("61",)),
    rule(9, "ANZAHL-AU holds the number of reported periods.", page=140, kind=CONTENT),
    rule(10, "The insurer checks the request within 3 days.", page=137, actor=I),
    rule(11, "A request needs at least one earlier period.", page=136, verified=False),
    rule(12, "Every message carries the sender's Betriebsnummer.", page=20, codes=(), section="2.2.2.5"),
    rule(13, "Reason 42 asks for the end of the benefit.", page=138, codes=("42",)),
]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "rules.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def reasons(p, rule_id) -> str:
    return "; ".join(next(e.reasons for e in p.excluded if e.rule.id == rule_id))


# --- picking ------------------------------------------------------------------------------------

def test_picks_exactly_the_employer_preconditions_for_41(conn):
    p = pick(conn, "REQ-41-PRECONDITION")
    assert p.rule_ids == [f"R-3.12-00{n}" for n in range(1, 8)]


def test_every_left_out_rule_about_41_has_a_reason(conn):
    p = pick(conn, "REQ-41-PRECONDITION")
    assert {e.rule.id for e in p.excluded} == {"R-3.12-009", "R-3.12-010", "R-3.12-011", "R-2.2.2.5-012"}
    assert "a content rule, not a precondition" in reasons(p, "R-3.12-009")
    assert "duty of the insurer" in reasons(p, "R-3.12-010")
    assert "quote not verified" in reasons(p, "R-3.12-011")
    assert "applies to every message" in reasons(p, "R-2.2.2.5-012")
    assert p.other_messages == 2  # reason 61 and reason 42


def test_a_rule_can_fail_several_filters(conn):
    db.save_requirement(conn, rule(14, "Insurer content rule, unverified.", kind=CONTENT, actor=I, verified=False))
    r = reasons(pick(conn, "REQ-41-PRECONDITION"), "R-3.12-014")
    assert "insurer" in r and "content rule" in r and "not verified" in r


def test_general_rules_can_be_included(conn):
    p = pick(conn, "REQ-41-PRECONDITION", include_general=True)
    assert "R-2.2.2.5-012" in p.rule_ids
    assert p.rule_ids[0] == "R-2.2.2.5-012"  # page 20 comes first


def test_page_order_not_id_order(conn):
    db.save_requirement(conn, rule(15, "A precondition printed early.", page=134))
    assert pick(conn, "REQ-41-PRECONDITION").rule_ids[0] == "R-3.12-015"


def test_warnings_for_single_votes_and_contradictions(conn):
    w = pick(conn, "REQ-41-PRECONDITION").warnings()
    assert any(x.startswith("R-3.12-005: found in only 1") for x in w)
    assert any(x.startswith("R-3.12-006: Contradiction with R-3.12-007") for x in w)
    assert any(x.startswith("R-3.12-007: Contradiction with R-3.12-006") for x in w)
    assert not any(x.startswith("R-3.12-001") for x in w)


def test_section_limit(conn, monkeypatch):
    monkeypatch.setitem(rule_picker.CARDS, "REQ-41-X", CardSpec("REQ-41-X", "test card", "41", PRE, sections=("9.9",)))
    with pytest.raises(PickError, match="all excluded"):
        pick(conn, "REQ-41-X")


# --- errors -------------------------------------------------------------------------------------

def test_empty_database(tmp_path):
    with pytest.raises(PickError, match="run the extractor first"):
        pick(db.connect(tmp_path / "empty.db"), "REQ-41-PRECONDITION")


def test_unknown_card(conn):
    with pytest.raises(PickError, match="REQ-41-PRECONDITION"):
        pick(conn, "REQ-99")


def test_nothing_passes(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.save_requirement(c, rule(1, "Only an insurer rule.", actor=I))
    with pytest.raises(PickError, match="1 rule"):
        pick(c, "REQ-41-PRECONDITION")


def test_card_spec_needs_a_real_reason_code():
    with pytest.raises(ValueError):
        CardSpec("REQ-40-X", "test card", "40", PRE)


# --- the pick fixes what the card may cite ------------------------------------------------------------

def gold_on_real_ids(p) -> CriteriaCard:
    """The gold card with placeholder ids 90n replaced by the picked ids 00n, built on the pick's skeleton."""
    g = gold.load("REQ-41-PRECONDITION").model_dump_json()
    for n in range(1, 8):
        g = g.replace(f"R-3.12-90{n}", f"R-3.12-00{n}")
    data = CriteriaCard.model_validate_json(g).model_dump()
    return CriteriaCard.model_validate(data | p.skeleton())


def test_gold_card_rebuilt_on_the_pick_is_valid_and_correct(conn):
    p = pick(conn, "REQ-41-PRECONDITION")
    card = gold_on_real_ids(p)
    assert card.source_rule_ids == p.rule_ids
    m = gold.mark(card, gold.load("REQ-41-PRECONDITION"))
    assert m.all_correct, m.summary()


def test_card_cannot_cite_a_rule_the_picker_left_out(conn):
    p = pick(conn, "REQ-41-PRECONDITION")
    data = gold_on_real_ids(p).model_dump()
    data["criteria"][0]["source_rule_ids"] = ["R-3.12-011"]  # the unverified rule
    with pytest.raises(ValueError, match="R-3.12-011 is cited but is not in source_rule_ids"):
        CriteriaCard.model_validate(data)


def test_rules_hash_follows_the_picked_rules_only(conn):
    before = pick(conn, "REQ-41-PRECONDITION").rules_hash()
    db.save_requirement(conn, NOISE[1].model_copy(update={"statement": "changed excluded rule"}))
    assert pick(conn, "REQ-41-PRECONDITION").rules_hash() == before
    db.save_requirement(conn, GOLD_RULES[2].model_copy(update={"quote": "die Zeiten mindestens 31 Tage umfassen"}))
    assert pick(conn, "REQ-41-PRECONDITION").rules_hash() != before


# --- command line ---------------------------------------------------------------------------------------

def test_cli(conn, tmp_path, capsys):
    assert rule_picker.main(["--db", str(tmp_path / "rules.db")]) == 0
    out = capsys.readouterr().out
    assert "7 rule(s) picked" in out and "✘ R-3.12-011" in out and "2 rule(s) for other messages" in out


def test_cli_empty_database(tmp_path, capsys):
    assert rule_picker.main(["--db", str(tmp_path / "none.db")]) == 1
    assert "run the extractor first" in capsys.readouterr().err
