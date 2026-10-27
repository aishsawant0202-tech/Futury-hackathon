"""Card guards: each guard fires on the mistake it is for, and stays quiet on a correct card."""
import pytest

from specpilot import db, llm
from specpilot.agents.translator import CardDraft, build_card, translate
from specpilot.tools import card_guards
from specpilot.tools.card_guards import limits, numbers, quote_in
from specpilot.tools.rule_picker import CARDS, Pick
from tests.test_rule_picker import GOLD_RULES, NOISE, rule
from tests.test_translator import FakeModel, good_answer

PICK = Pick(CARDS["REQ-41-PRECONDITION"], GOLD_RULES)


def findings(answer: dict, rules=GOLD_RULES):
    card = build_card(Pick(PICK.spec, rules), CardDraft.model_validate(answer), "test")
    return card_guards.check(card, rules)


def with_c(n, **changes) -> dict:
    cs = good_answer()["criteria"]
    cs[n - 1] = cs[n - 1] | changes
    return good_answer(criteria=cs)


def guards(fs) -> list[tuple[str, str, str]]:
    return [(g.severity, g.guard, g.where) for g in fs]


# --- reading German limits ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("mindestens 30 Tage", [(">=", 30, "days")]),
    ("nicht mehr als 6 Monate", [("<=", 6, "months")]),      # not "mehr als" > 6
    ("nicht weniger als 30 Tage", [(">=", 30, "days")]),
    ("mehr als 42 Tage", [(">", 42, "days")]),
    ("höchstens 3 Wochen", [("<=", 3, "weeks")]),
    ("maximal innerhalb der letzten 12 Monate", [("<=", 12, "months")]),
    ("weniger als 30 Kalendertage", [("<", 30, "days")]),
    ("bis zu 3 Arbeitstagen", [("<=", 3, "days")]),
    ("darüber hinaus 5 Tage", []),                            # "über" inside a word
    ("mindestens 30 Tage, jedoch höchstens 60 Tage", [(">=", 30, "days"), ("<=", 60, "days")]),
    ("Arbeitsunfähigkeitsbescheinigung liegt vor", []),
])
def test_limits(text, expected):
    assert [(lim.op, lim.number, lim.unit) for lim in limits(text)] == expected


def test_numbers_with_units():
    assert numbers("30 Tage und 6 Monate, Abschnitt 12") == [(30, "days"), (6, "months"), (12, "")]


@pytest.mark.parametrize("quote, ok", [
    ("eine Arbeitsunfähigkeitsbescheinigung vorliegt", True),
    ("eine Arbeitsunfähigkeits-\nbescheinigung  vorliegt", True),     # line-break hyphen and spaces
    ("Die Anfrage ... Arbeitsunfähigkeitsbescheinigung vorliegt", True),
    ("Arbeitsunfähigkeitsbescheinigung … Die Anfrage", False),       # parts out of order
    ("eine ärztliche Bescheinigung vorliegt", False),                # not the rule's words
])
def test_quote_in(quote, ok):
    assert quote_in(quote, GOLD_RULES[0].quote) is ok


# --- a correct card -----------------------------------------------------------------------------

def test_correct_card_has_no_findings():
    assert findings(good_answer()) == []


# --- errors ---------------------------------------------------------------------------------------

def test_made_up_quote():
    fs = findings(with_c(1, quote="eine ärztliche Bescheinigung vorliegt"))
    assert guards(fs)[0] == ("error", "quote_in_rule", "C1")
    assert "not in R-3.12-001" in fs[0].message


@pytest.mark.parametrize("op, value", [(">", 30), (">=", 31), ("<", 30), ("between", [30, 60])])
def test_limit_words_disagree(op, value):
    fs = findings(with_c(3, op=op, value=value))
    assert ("error", "limit", "C3") in guards(fs)
    assert "„mindestens 30 tage“ (>= 30)" in next(g.message for g in fs if g.guard == "limit")


def test_same_meaning_is_not_an_error():
    assert findings(with_c(3, op=">", value=29)) == []


def test_number_without_limit_word():
    rules = [*GOLD_RULES[:2], rule(3, "30 days.", quote="Die Vorerkrankungszeiten umfassen zusammen mit der aktuellen AU-Zeit 30 Tage."), *GOLD_RULES[3:]]
    ok = findings(with_c(3, quote="zusammen mit der aktuellen AU-Zeit 30 Tage"), rules)
    assert guards(ok) == [("warning", "limit_unchecked", "C3")]
    wrong = findings(with_c(3, value=31, quote="zusammen mit der aktuellen AU-Zeit 30 Tage"), rules)
    assert ("error", "limit", "C3") in guards(wrong)


def test_months_are_not_days():
    """A 12-month window must not be read as a 12-day limit on total_days_12m."""
    rules = [*GOLD_RULES[:2], rule(3, "x", quote="Zu zählen sind die AU-Zeiten maximal innerhalb der letzten 12 Monate."),
             *GOLD_RULES[3:]]
    fs = findings(with_c(3, quote="AU-Zeiten maximal innerhalb der letzten 12 Monate"), rules)
    assert ("warning", "limit_unchecked", "C3") in guards(fs)
    assert not any(g.guard == "limit" for g in fs)
    assert not any(g.guard == "period" for g in fs)  # 12 months is total_days_12m's own window


# --- warnings -------------------------------------------------------------------------------------

def test_wrong_month_period_for_the_fact():
    rules = [GOLD_RULES[0], rule(2, "x", quote="Innerhalb der letzten 12 Monate muss eine bescheinigte Vorerkrankung liegen."), *GOLD_RULES[2:]]
    fs = findings(with_c(2, quote="Innerhalb der letzten 12 Monate muss eine bescheinigte Vorerkrankung liegen"), rules)
    assert guards(fs) == [("warning", "period", "C2")]
    assert "12 month(s) but prior_certified_6m uses 6 months" in fs[0].message


def test_quote_without_any_word_of_the_fact():
    fs = findings(with_c(5, fact="current_open_end"))  # quote is about Minijobs, fact is the open end
    assert ("warning", "fact_terms", "C5") in guards(fs)


def test_implausible_value():
    rules = [*GOLD_RULES[:2], rule(3, "x", quote="Die Zeiten müssen mindestens 500 Tage umfassen."), *GOLD_RULES[3:]]
    fs = findings(with_c(3, value=500, quote="mindestens 500 Tage umfassen"), rules)
    assert guards(fs) == [("warning", "plausible", "C3")]


def test_two_criteria_on_one_fact():
    a = good_answer()
    a["criteria"].append(a["criteria"][2] | {"op": "<=", "value": 60})
    fs = findings(a)
    assert ("warning", "same_fact", "C3+C6") in guards(fs)


def test_flagged_rule_with_a_single_reading():
    a = good_answer(unused_rules=[{"rule_id": "R-3.12-005", "reason": "counting"}, {"rule_id": "R-3.12-007", "reason": "reported"}])
    a["criteria"].append({"fact": "prior_certified_6m", "op": "=", "value": True, "source_rule_ids": ["R-3.12-006"],
                          "quote": "nicht mehr als 6 Monate liegen"})
    fs = findings(a)
    assert ("warning", "contradiction", "C6") in guards(fs)
    assert "Contradiction with R-3.12-007" in next(g.message for g in fs if g.guard == "contradiction")


def test_alternative_readings_are_checked_too():
    alt = {"fact": "total_days_12m", "op": ">", "value": 30, "source_rule_ids": ["R-3.12-003"],
           "quote": "mehr als 30 Tage", "label": "other"}
    fs = findings(with_c(3, label="main", ambiguity="test", alternatives=[alt]))
    assert ("error", "quote_in_rule", "C3 reading 'other'") in guards(fs)


def test_missing_fact():
    a = good_answer(unused_rules=[{"rule_id": "R-3.12-005", "reason": "counting"}, {"rule_id": "R-3.12-006", "reason": "reported"}],
                    missing_facts=[{"rule_ids": ["R-3.12-007"], "quote": "maximal innerhalb der letzten 12 Monate",
                                    "needed": "how many months back periods are reported"}])
    assert guards(findings(a)) == [("warning", "missing_fact", "card")]


def test_errors_come_first():
    fs = findings(with_c(1, quote="erfunden und nicht im Text"))
    assert fs[0].severity == "error" and all(g.severity == "warning" for g in fs[1:])


# --- in the translator ----------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "rules.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def test_guard_errors_are_sent_back_once(conn, monkeypatch):
    fake = FakeModel(with_c(3, op=">"), good_answer())
    monkeypatch.setattr(llm, "ask_json", fake)
    card = translate(conn, "REQ-41-PRECONDITION", "fake", cross_check=False)
    assert len(fake.users) == 2
    assert "does not match the rule text" in fake.users[1] and "[limit] C3" in fake.users[1]
    assert card.criteria[2].op == ">=" and not any(f.startswith("[") for f in card.flags)


def test_remaining_guard_errors_become_flags_not_failures(conn, monkeypatch):
    fake = FakeModel(with_c(3, op=">"))  # keeps making the same mistake
    monkeypatch.setattr(llm, "ask_json", fake)
    card = translate(conn, "REQ-41-PRECONDITION", "fake", cross_check=False)
    assert len(fake.users) == 2  # sent back once only
    assert any(f.startswith("[limit] C3: card says total_days_12m > 30") for f in card.flags)
    actions = [e.action for e in db.list_audit(conn, "REQ-41-PRECONDITION")]
    assert any("guard error(s), sent back" in a for a in actions)
    assert any("1 guard error(s), 0 warning(s)" in a for a in actions)


def test_warnings_alone_are_not_sent_back(conn, monkeypatch):
    fake = FakeModel(with_c(5, fact="current_open_end", op="=", value=False))
    monkeypatch.setattr(llm, "ask_json", fake)
    card = translate(conn, "REQ-41-PRECONDITION", "fake", cross_check=False)
    assert len(fake.users) == 1
    assert any(f.startswith("[fact_terms] C5") for f in card.flags)
