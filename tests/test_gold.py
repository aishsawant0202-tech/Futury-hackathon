"""The gold card for reason 41 and the marking that compares translator cards with it."""
import random

import pytest

from specpilot.criteria import CriteriaCard
from specpilot.tools import gold
from specpilot.tools.reference_model import P135, P136, expected
from tests.test_facts import P, as_case, random_scenario, scenario

GOLD = gold.load("REQ-41-PRECONDITION")


def variant(**changes) -> CriteriaCard:
    """The gold card with some fields changed, revalidated (as a translator card would be).
    A rule no criterion cites any more is moved to unused_rules, so the card stays complete."""
    data = GOLD.model_dump() | {"produced_by": "test"} | changes
    cited = {r for c in data["criteria"] for r in c["source_rule_ids"]}
    cited |= {u["rule_id"] for u in data["unused_rules"]} | {r for m in data["missing_facts"] for r in m["rule_ids"]}
    data["unused_rules"] = data["unused_rules"] + [
        {"rule_id": r, "reason": "test: criterion dropped"} for r in data["source_rule_ids"] if r not in cited]
    return CriteriaCard.model_validate(data)


def criteria(*edits) -> list[dict]:
    """Gold criteria as dicts; each edit is (C id, {changes}) or (C id, None) to drop it. Renumbered."""
    cs = {c.id: c.model_dump() for c in GOLD.criteria}
    for cid, change in edits:
        if change is None:
            del cs[cid]
        else:
            cs[cid] = cs[cid] | change
    out = list(cs.values())
    for n, c in enumerate(out, start=1):
        c["id"] = f"C{n}"
    return out


# --- the gold card itself ---------------------------------------------------------------------

def test_gold_card_loads_and_is_a_draft_with_placeholders():
    assert GOLD.status.value == "draft" and GOLD.produced_by == "hand"
    assert gold.placeholder_ids(GOLD) == [f"R-3.12-90{n}" for n in range(1, 8)]
    assert any("PLACEHOLDER" in f for f in GOLD.flags)


def test_every_picked_rule_is_used_or_explained():
    used = {r for c in GOLD.criteria for r in c.source_rule_ids}
    unused = {u.rule_id for u in GOLD.unused_rules}
    assert used | unused == set(GOLD.source_rule_ids) and not used & unused


@pytest.mark.parametrize("s, send", [
    (scenario(), True),                                                        # all conditions met
    (scenario(current=P("2026-10-01", "2026-10-17", certified=False)), False),  # no sick note
    (scenario(previous=[P("2026-02-01", "2026-03-20")]), False),                # nothing in the 6 months before
    (scenario(previous=[P("2026-06-01", "2026-06-12")]), False),                # 17 + 12 = 29 days
    (scenario(previous=[P("2026-06-01", "2026-06-13")]), True),                 # exactly 30 days
    (scenario(statutory_insured=False), False),                                 # private insurance
    (scenario(marginal_employment=True), False),                                # Minijob
])
def test_gold_card_on_the_reference_decision_table(s, send):
    assert GOLD.satisfied(s) is send
    assert expected(as_case(s)).message_expected is send


@pytest.mark.parametrize("reading", [P135, P136])
def test_gold_card_agrees_with_reference_model_on_random_people(reading):
    rng = random.Random(135)
    for _ in range(3000):
        s = random_scenario(rng)
        assert GOLD.satisfied(s) == expected(as_case(s), reading).message_expected


@pytest.mark.parametrize("cid", ["C1", "C2", "C3", "C4", "C5"])
def test_every_gold_criterion_is_needed(cid):
    """Drop one criterion: the card must then disagree with the reference model for someone."""
    weaker = variant(criteria=criteria((cid, None)))
    rng = random.Random(9)
    disagreements = sum(weaker.satisfied(s) != expected(as_case(s)).message_expected
                        for s in (random_scenario(rng) for _ in range(3000)))
    assert disagreements > 0, f"{cid} makes no difference: the gold card would be over-complete"


# --- marking ------------------------------------------------------------------------------------

def results(m) -> dict[str, str]:
    return {c.gold_id: c.result for c in m.criteria}


def test_gold_against_itself_is_all_exact():
    m = gold.mark(GOLD, GOLD)
    assert m.all_correct and m.correct == 5
    assert set(results(m).values()) == {"exact"}
    assert not m.citations_checked  # placeholders


def test_same_meaning_counts_as_equivalent():
    cand = variant(criteria=criteria(("C3", {"op": ">", "value": 29}), ("C5", {"op": "!=", "value": True})))
    m = gold.mark(cand, GOLD)
    assert results(m) == {"C1": "exact", "C2": "exact", "C3": "equivalent", "C4": "exact", "C5": "equivalent"}
    assert m.all_correct


@pytest.mark.parametrize("change", [
    {"op": ">", "value": 30},    # 30 itself no longer counts
    {"op": ">=", "value": 31},
    {"op": "<", "value": 30},
    {"op": "between", "value": [30, 60]},
])
def test_different_meaning_is_wrong(change):
    m = gold.mark(variant(criteria=criteria(("C3", change))), GOLD)
    assert results(m)["C3"] == "wrong" and not m.all_correct


def test_missing_and_extra_criteria():
    cand = variant(criteria=criteria(("C4", None)) + [{
        "id": "C5", "fact": "current_open_end", "op": "=", "value": False,
        "source_rule_ids": ["R-3.12-905"], "quote": "test"}],
        unused_rules=[u.model_dump() for u in GOLD.unused_rules if u.rule_id != "R-3.12-905"])
    m = gold.mark(cand, GOLD)
    assert results(m)["C4"] == "missing"
    assert m.extra == ["C5 current_open_end = false"]
    assert m.correct == 4 and not m.all_correct


def test_order_does_not_matter():
    reordered = list(reversed(criteria()))
    for n, c in enumerate(reordered, start=1):
        c["id"] = f"C{n}"
    m = gold.mark(variant(criteria=reordered), GOLD)
    assert m.all_correct
    assert m.criteria[0].found.startswith("C5 current_certified")


def test_card_level_differences():
    m = gold.mark(variant(logic="OR", reason_codes=["41", "61"]), GOLD)
    assert m.correct == 5 and not m.all_correct
    assert any("logic" in p for p in m.problems) and any("reason codes" in p for p in m.problems)


def test_an_unrequested_alternative_reading_is_wrong():
    alt = {"fact": "total_days_12m", "op": ">", "value": 30, "source_rule_ids": ["R-3.12-903"],
           "quote": "test", "label": "invented reading"}
    cand = variant(criteria=criteria(("C3", {"label": "main", "ambiguity": "test", "alternatives": [alt]})))
    m = gold.mark(cand, GOLD)
    assert results(m)["C3"] == "wrong"
    assert "alternative readings differ" in m.criteria[2].note


def test_citations_are_marked_once_gold_has_real_ids():
    real_ids = {f"R-3.12-90{n}": f"R-3.12-00{n}" for n in range(1, 8)}
    dumped = GOLD.model_dump_json()
    for old, new in real_ids.items():
        dumped = dumped.replace(old, new)
    real_gold = CriteriaCard.model_validate_json(dumped)
    assert gold.placeholder_ids(real_gold) == []
    data = real_gold.model_dump()
    data["criteria"][0]["source_rule_ids"] = ["R-3.12-002"]  # C1 cites the wrong rule
    data["unused_rules"].append({"rule_id": "R-3.12-001", "reason": "test: no longer cited"})
    cand = CriteriaCard.model_validate(data)
    m = gold.mark(cand, real_gold)
    assert m.citations_checked
    assert "cites ['R-3.12-002'], gold cites ['R-3.12-001']" in m.criteria[0].note


def test_summary_text():
    text = gold.mark(variant(criteria=criteria(("C3", {"value": 31}))), GOLD).summary()
    assert "4/5 sub-criteria correct" in text and "NOT CORRECT" in text and "wrong" in text


def test_unknown_gold_card():
    with pytest.raises(FileNotFoundError, match="REQ-41-PRECONDITION"):
        gold.load("REQ-99")
