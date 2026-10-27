"""Branch maker: all 2^n combinations, in binary order, one full set per reading."""
from collections import Counter
from itertools import product

import pytest

from specpilot.branches import BranchSet, Kind, Target
from specpilot.criteria import CriteriaCard
from specpilot.tools import gold
from specpilot.tools.branch_maker import BranchError, free_facts, make_branches, reading_choices, size_warnings

GOLD = gold.load("REQ-41-PRECONDITION")

# Valid conditions to build test cards of any size (number conditions may repeat on one fact)
POOL = [("current_certified", "=", True), ("prior_certified_6m", "=", True), ("statutory_insured", "=", True),
        ("marginal_employment", "=", False), ("current_open_end", "=", False)]


def card(n: int, logic="AND", alternatives: dict[int, dict] | None = None) -> CriteriaCard:
    conds = [POOL[i] if i < len(POOL) else ("total_days_12m", ">=", 10 * i) for i in range(n)]
    criteria = []
    for i, (fact, op, value) in enumerate(conds, start=1):
        c = {"id": f"C{i}", "fact": fact, "op": op, "value": value, "source_rule_ids": ["R-3.12-001"], "quote": "Testzitat"}
        if alternatives and i in alternatives:
            c |= {"label": "main", "ambiguity": "test", "alternatives": [alternatives[i]]}
        criteria.append(c)
    return CriteriaCard.model_validate({"id": "REQ-41-TEST", "title": "test card", "reason_codes": ["41"],
                                        "kind": "precondition", "logic": logic, "criteria": criteria,
                                        "source_rule_ids": ["R-3.12-001"]})


def alt(fact, op, value, label):
    return {"fact": fact, "op": op, "value": value, "source_rule_ids": ["R-3.12-001"], "quote": "Testzitat", "label": label}


# --- the reason 41 card ---------------------------------------------------------------------------

def test_reason_41_card_gives_32_branches_in_binary_order():
    s = make_branches(GOLD)
    assert isinstance(s, BranchSet) and len(s.branches) == 32
    expected = ["".join("✔" if t is Target.MET else "✘" for t in p) for p in product((Target.MET, Target.FAILED), repeat=5)]
    assert [b.pattern for b in s.branches] == expected
    assert s.branches[0].pattern == "✔✔✔✔✔" and s.branches[-1].pattern == "✘✘✘✘✘"
    assert [b.branch_id for b in s.branches[:2]] == ["REQ-41-PRECONDITION-B01", "REQ-41-PRECONDITION-B02"]
    assert s.branches[1].pattern == "✔✔✔✔✘"  # C5 changes fastest


def test_targets_are_the_cards_conditions():
    s = make_branches(GOLD)
    for b in s.branches:
        for t, c in zip(b.targets, GOLD.criteria):
            assert (t.criterion_id, t.fact, t.op, t.value) == (c.id, c.fact, c.op, c.value)


def test_card_identity_and_free_facts():
    s = make_branches(GOLD)
    assert (s.card_id, s.card_version, s.card_hash) == (GOLD.id, GOLD.version, GOLD.content_hash())
    assert all(b.card_hash == GOLD.content_hash() for b in s.branches)
    # the precondition card leaves open current_open_end and the chain facts (DBVO content)
    assert all(b.free_facts == ["current_open_end", "latest_gap_within_6m", "chain_link_exactly_6m", "certified_prior_count", "prior_ends_before_12m", "uncertified_inside", "prior_gaps_under_180d"] for b in s.branches)
    assert free_facts(GOLD) == ["current_open_end", "latest_gap_within_6m", "chain_link_exactly_6m", "certified_prior_count", "prior_ends_before_12m", "uncertified_inside", "prior_gaps_under_180d"]


def test_kinds_and_all_true_for_5_conditions():
    s = make_branches(GOLD)
    assert Counter(b.kind for b in s.branches) == {Kind.ALL_MET: 1, Kind.SINGLE_FAIL: 5, Kind.MULTI_FAIL: 25, Kind.NONE_MET: 1}
    assert [b.number for b in s.branches if b.all_true] == [1]


def test_same_card_same_branches():
    assert make_branches(GOLD).model_dump_json() == make_branches(GOLD).model_dump_json()


def test_new_card_version_new_fingerprint():
    edited = CriteriaCard.model_validate(GOLD.model_dump() | {"version": 2})
    assert make_branches(edited).card_hash != make_branches(GOLD).card_hash


# --- any size -----------------------------------------------------------------------------------

@pytest.mark.parametrize("n", range(1, 9))
def test_two_to_the_n(n):
    s = make_branches(card(n))
    assert len(s.branches) == 2 ** n
    assert len({b.pattern for b in s.branches}) == 2 ** n
    assert s.branches[0].kind is Kind.ALL_MET and s.branches[-1].kind is (Kind.NONE_MET if n > 1 else Kind.SINGLE_FAIL)
    assert sum(b.kind is Kind.SINGLE_FAIL for b in s.branches) == n


@pytest.mark.parametrize("n", [1, 3, 6])
def test_or_logic(n):
    s = make_branches(card(n, "OR"))
    assert sum(b.all_true for b in s.branches) == 2 ** n - 1
    assert not s.branches[-1].all_true


def test_ids_get_wider_for_big_cards():
    s = make_branches(card(10))
    assert s.branches[0].branch_id == "REQ-41-TEST-B0001" and s.branches[-1].branch_id == "REQ-41-TEST-B1024"


# --- readings --------------------------------------------------------------------------------------

def test_one_ambiguous_condition_doubles_the_set():
    c = card(3, alternatives={3: alt("statutory_insured", "!=", False, "other")})
    assert reading_choices(c) == [{}, {"C3": "other"}]
    s = make_branches(c)
    assert len(s.branches) == 16 and s.readings == [{}, {"C3": "other"}]
    main, other = s.branches[:8], s.branches[8:]
    assert all(b.reading == {} and "-R" not in b.branch_id for b in main)
    assert all(b.reading == {"C3": "other"} and "-R2-B" in b.branch_id for b in other)
    assert {b.targets[2].op for b in main} == {"="} and {b.targets[2].op for b in other} == {"!="}
    # the pattern and all_true depend on met/failed only, not on the reading
    assert [(b.pattern, b.all_true) for b in main] == [(b.pattern, b.all_true) for b in other]


def test_two_ambiguous_conditions_give_four_readings():
    c = card(6, alternatives={6: alt("total_days_12m", ">", 50, "strict"), 2: alt("prior_certified_6m", "!=", False, "neg")})
    choices = reading_choices(c)
    assert choices == [{}, {"C6": "strict"}, {"C2": "neg"}, {"C2": "neg", "C6": "strict"}]
    s = make_branches(c)
    assert len(s.branches) == 4 * 64
    assert s.branches[-1].branch_id == "REQ-41-TEST-R4-B64"


@pytest.mark.parametrize("n", [1, 3, 5, 7])
def test_free_facts_are_exactly_the_facts_the_card_leaves_open(n):
    c = card(n)
    on_card = {x.fact for x in c.criteria}
    assert set(free_facts(c)) | on_card == set(gold.facts.FACTS) and not set(free_facts(c)) & on_card


# --- impossible branches -----------------------------------------------------------------------------

def custom(*conds, logic="AND") -> CriteriaCard:
    """A card from (fact, op, value) conditions, which may repeat a fact."""
    return CriteriaCard.model_validate({
        "id": "REQ-41-TEST", "title": "test card", "reason_codes": ["41"], "kind": "precondition", "logic": logic,
        "criteria": [{"id": f"C{i}", "fact": f, "op": op, "value": v, "source_rule_ids": ["R-3.12-001"], "quote": "Testzitat"}
                     for i, (f, op, v) in enumerate(conds, start=1)],
        "source_rule_ids": ["R-3.12-001"]})


def status(s) -> dict[str, str]:
    return {b.pattern: b.status.value for b in s.branches}


def test_reason_41_card_has_no_impossible_branch():
    assert all(b.status.value == "possible" and b.reason == "" for b in make_branches(GOLD).branches)


def test_two_limits_on_one_number():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 60)))
    assert status(s) == {"✔✔": "possible", "✔✘": "possible", "✘✔": "possible", "✘✘": "impossible"}
    assert s.branches[-1].reason == ("C1 (total_days_12m >= 30) must be FAILED and C2 (total_days_12m <= 60) must be FAILED: "
                                     "no total_days_12m value in the realistic range 1-400 days does all of that")
    assert "(3 possible, 1 impossible)" in s.summary()


def test_nested_limits():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", ">=", 60)))
    assert status(s) == {"✔✔": "possible", "✔✘": "possible", "✘✔": "impossible", "✘✘": "possible"}


def test_one_condition_against_the_realistic_range():
    s = make_branches(custom(("total_days_12m", ">=", 1)))
    assert status(s) == {"✔": "possible", "✘": "impossible"}  # 0 days cannot happen
    assert "realistic range 1-400 days does that" in s.branches[-1].reason
    assert status(make_branches(custom(("total_days_12m", ">", 400)))) == {"✔": "impossible", "✘": "possible"}


def test_yes_no_fact_twice():
    s = make_branches(custom(("current_certified", "=", True), ("current_certified", "=", False)))
    assert status(s) == {"✔✔": "impossible", "✔✘": "possible", "✘✔": "possible", "✘✘": "impossible"}


def test_between():
    s = make_branches(custom(("total_days_12m", "between", [30, 60]), ("total_days_12m", ">", 60)))
    assert status(s) == {"✔✔": "impossible", "✔✘": "possible", "✘✔": "possible", "✘✘": "possible"}


def test_clashes_on_two_facts_are_both_explained():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 20),
                             ("current_certified", "=", True), ("current_certified", "!=", True)))
    b = next(b for b in s.branches if b.pattern == "✔✔✔✔")
    assert b.reason.count("no ") == 2 and "total_days_12m" in b.reason and "current_certified" in b.reason


def test_different_facts_never_clash_here():
    """Same-fact check only: different facts are left to the value finder (Station 4)."""
    s = make_branches(custom(*POOL[:4]))
    assert all(b.status.value == "possible" for b in s.branches)


def test_impossible_branches_are_kept():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 60)))
    assert len(s.branches) == 4  # still 2^n
    assert make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 60)), check_possible=False
                         ).branches[-1].status.value == "possible"


def test_no_false_impossible_on_random_cards():
    """Independent check: brute-force every value from 0 to 401 for each branch of random cards."""
    import random
    from specpilot.tools import facts
    rng = random.Random(3)
    ops = [">", ">=", "<", "<=", "=", "!="]
    for _ in range(150):
        conds = []
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.2:
                lo = rng.randint(0, 400)
                conds.append(("total_days_12m", "between", [lo, rng.randint(lo, 401)]))
            else:
                conds.append(("total_days_12m", rng.choice(ops), rng.choice([0, 1, 2, 29, 30, 31, 200, 399, 400, 401])))
        for b in make_branches(custom(*conds)).branches:
            exists = any(all(facts.holds(v, t.op, t.value) == (t.target is Target.MET) for t in b.targets)
                         for v in range(1, 401))
            assert (b.status.value == "possible") == exists, (conds, b.pattern)


# --- edge branches -------------------------------------------------------------------------------------

def edges(s) -> dict[str, list[int]]:
    """{parent pattern: [pinned values]} for every edge branch."""
    out: dict[str, list[int]] = {}
    for b in s.branches:
        if b.is_edge:
            out.setdefault(b.pattern, []).append(next(t.edge_value for t in b.targets if t.at_edge))
    return out


def test_edges_are_off_by_default():
    assert not any(b.is_edge for b in make_branches(GOLD).branches)


def test_reason_41_edges_30_and_29():
    s = make_branches(GOLD, edges=True)
    assert len(s.branches) == 34 and "+ 2 edge branch(es)" in s.summary()
    assert edges(s) == {"✔✔✔✔✔": [30], "✔✔✘✔✔": [29]}
    e = [b for b in s.branches if b.is_edge]
    assert [(b.branch_id, b.edge_of) for b in e] == [("REQ-41-PRECONDITION-B01-E1", "REQ-41-PRECONDITION-B01"),
                                                     ("REQ-41-PRECONDITION-B05-E1", "REQ-41-PRECONDITION-B05")]
    assert s.branches[1].branch_id == e[0].branch_id  # right after its parent


@pytest.mark.parametrize("op, value, met, failed", [
    (">=", 30, [30], [29]),
    (">", 30, [31], [30]),
    ("<=", 30, [30], [31]),
    ("<", 30, [29], [30]),
    ("=", 30, [30], [29, 31]),
    ("!=", 30, [29, 31], [30]),
    ("between", [30, 60], [30, 60], [29, 61]),
])
def test_edge_values_for_every_operator(op, value, met, failed):
    s = make_branches(custom(("total_days_12m", op, value)), edges=True)
    assert edges(s) == {"✔": met, "✘": failed}


def test_edges_stay_in_the_realistic_range():
    s = make_branches(custom(("total_days_12m", ">=", 1)), edges=True)
    assert edges(s) == {"✔": [1]}  # failing would need 0 days: that branch is impossible, no edge
    s = make_branches(custom(("total_days_12m", "<=", 400)), edges=True)
    assert edges(s) == {"✔": [400]}  # 401 is outside the realistic range
    # a possible branch with one limit outside the range: failing between [1, 60] is possible (61+),
    # but its lower edge would be 0 days, so only 61 is made
    s = make_branches(custom(("total_days_12m", "between", [1, 60])), edges=True)
    assert edges(s) == {"✔": [1, 60], "✘": [61]}


def test_edges_respect_the_other_conditions_on_the_same_fact():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", ">=", 31)), edges=True)
    # all met: 30 would fail C2, so only C2's edge (31) is made
    assert edges(s)["✔✔"] == [31]
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 60)), edges=True)
    assert edges(s) == {"✔✔": [30, 60], "✔✘": [61], "✘✔": [29]}


def test_only_all_met_and_single_fail_branches_get_edges():
    s = make_branches(card(7), edges=True)  # C6, C7 are number conditions (>= 50, >= 60)
    parents = {b.edge_of for b in s.branches if b.is_edge}
    kinds = {next(p for p in s.branches if p.branch_id == pid).kind for pid in parents}
    assert kinds <= {Kind.ALL_MET, Kind.SINGLE_FAIL}


def test_yes_no_conditions_get_no_edges():
    assert not any(b.is_edge for b in make_branches(custom(*POOL), edges=True).branches)


def test_impossible_branches_get_no_edges():
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", ">=", 60)), edges=True)
    assert "✘✔" not in edges(s)  # impossible branch


def test_edges_for_every_reading():
    c = card(6, alternatives={6: alt("total_days_12m", ">", 50, "strict")})  # C6: >= 50 main, > 50 strict
    s = make_branches(c, edges=True)
    main = [b for b in s.branches if b.is_edge and b.reading == {}]
    strict = [b for b in s.branches if b.is_edge and b.reading == {"C6": "strict"}]
    assert [t.edge_value for b in main for t in b.targets if t.at_edge] == [50, 49]
    assert [t.edge_value for b in strict for t in b.targets if t.at_edge] == [51, 50]
    assert all("-R2-B" in b.branch_id for b in strict)


def test_edges_are_deterministic_and_pass_the_form():
    a, b = make_branches(GOLD, edges=True), make_branches(GOLD, edges=True)
    assert a.model_dump_json() == b.model_dump_json()
    assert BranchSet.model_validate_json(a.model_dump_json()) == a


# --- size limits ---------------------------------------------------------------------------------

def test_size_warnings():
    assert size_warnings(GOLD) == []
    assert "2^10 = 1,024 branches" in size_warnings(card(10))[0]
    two_readings = card(3, alternatives={3: alt("statutory_insured", "!=", False, "other")})
    assert "2 reading combinations x 8 = 16" in size_warnings(two_readings)[0]


def test_too_many_conditions_are_refused_unless_forced():
    with pytest.raises(BranchError, match="12 conditions x 1 reading"):
        make_branches(card(12))
    assert len(make_branches(card(12), force=True).branches) == 4096


def test_too_many_branches_across_readings_are_refused():
    c = card(11, alternatives={11: alt("total_days_12m", ">", 100, "strict"), 10: alt("total_days_12m", ">", 90, "s2")})
    with pytest.raises(BranchError, match="8,192 branches"):
        make_branches(c)
