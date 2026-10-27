"""Branch form: valid branches and sets build; incomplete or inconsistent ones are refused."""
from itertools import product

import pytest
from pydantic import ValidationError

from specpilot.branches import Branch, BranchSet, Kind, Status, Target, all_true_of, kind_of
from specpilot.tools import gold

CARD = gold.load("REQ-41-PRECONDITION")  # 5 conditions; only used for its conditions and fingerprint
M, F = Target.MET, Target.FAILED


def target(c, t: Target, **extra) -> dict:
    return {"criterion_id": c.id, "fact": c.fact, "op": c.op, "value": c.value, "target": t.value, **extra}


def branch(n: int, targets: tuple[Target, ...], logic="AND", reading=None, prefix="", **extra) -> dict:
    data = {
        "branch_id": f"{CARD.id}{prefix}-B{n:02d}", "number": n, "card_id": CARD.id, "card_version": CARD.version,
        "card_hash": CARD.content_hash(), "logic": logic, "reading": reading or {},
        "targets": [target(c, t) for c, t in zip(CARD.criteria, targets)],
        "all_true": all_true_of(logic, list(targets)), "kind": kind_of(list(targets)).value,
        "free_facts": ["current_open_end"],
    }
    return data | extra


def patterns(n=5):
    return list(product((M, F), repeat=n))  # binary order, C1 slowest: all met first, none met last


def full_set(logic="AND", **extra) -> dict:
    return {"card_id": CARD.id, "card_version": CARD.version, "card_hash": CARD.content_hash(), "logic": logic,
            "n_criteria": 5, "readings": [{}],
            "branches": [branch(i, p, logic) for i, p in enumerate(patterns(), start=1)], **extra}


# --- one branch -----------------------------------------------------------------------------------

def test_valid_branch_and_round_trip():
    b = Branch.model_validate(branch(2, (M, M, M, M, F)))
    assert b.pattern == "✔✔✔✔✘" and b.kind is Kind.SINGLE_FAIL and not b.all_true
    assert Branch.model_validate_json(b.model_dump_json()) == b
    assert b.targets[2].text() == "C3: total_days_12m >= 30 -> must be MET"


@pytest.mark.parametrize("targets, kind", [
    ((M,), Kind.ALL_MET), ((F,), Kind.SINGLE_FAIL), ((M, F), Kind.SINGLE_FAIL), ((F, F), Kind.NONE_MET),
    ((F, F, M), Kind.MULTI_FAIL), ((F, F, F), Kind.NONE_MET), ((M, M, M), Kind.ALL_MET),
])
def test_kind_of(targets, kind):
    assert kind_of(list(targets)) is kind


def test_all_true_follows_the_logic():
    assert all_true_of("AND", [M, M]) and not all_true_of("AND", [M, F])
    assert all_true_of("OR", [M, F]) and not all_true_of("OR", [F, F])


@pytest.mark.parametrize("changes, message", [
    ({"all_true": True}, "all_true=True does not follow"),
    ({"kind": "all_met"}, "kind all_met does not match"),
    ({"status": "impossible"}, "must say why"),
    ({"reason": "because"}, "a possible branch has no reason"),
    ({"free_facts": ["total_days_12m"]}, "cannot be free"),
    ({"free_facts": ["shoe_size"]}, "unknown fact"),
    ({"branch_id": "REQ-41-PRECONDITION-7"}, "branch_id"),
    ({"card_hash": "abc"}, "card_hash"),
    ({"edge_of": "REQ-41-PRECONDITION-B01"}, "edge targets and edge_of go together"),
    ({"extra": 1}, "Extra inputs"),
])
def test_inconsistent_branches_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        Branch.model_validate(branch(2, (M, M, M, M, F)) | changes)


def test_targets_must_be_valid_conditions_in_order():
    data = branch(1, (M,) * 5)
    data["targets"][2]["op"] = ">"
    data["targets"][2]["value"] = "30"
    with pytest.raises(ValidationError):
        Branch.model_validate(data)
    data = branch(1, (M,) * 5)
    data["targets"][0], data["targets"][1] = data["targets"][1], data["targets"][0]
    with pytest.raises(ValidationError, match="C1, C2, ... in order"):
        Branch.model_validate(data)


def test_impossible_branch_with_reason():
    b = Branch.model_validate(branch(32, (F,) * 5, status="impossible", reason="test: cannot exist"))
    assert b.status is Status.IMPOSSIBLE


# --- edge targets ------------------------------------------------------------------------------------

def edge_branch(t: Target, value: int, **extra) -> dict:
    targets = (M, M, t, M, M)
    data = branch(1, targets, branch_id=f"{CARD.id}-B01-E1", edge_of=f"{CARD.id}-B01")
    data["targets"][2] |= {"at_edge": True, "edge_value": value} | extra
    return data


@pytest.mark.parametrize("t, value", [(M, 30), (F, 29)])  # C3 is total_days_12m >= 30
def test_edge_values_at_the_limit(t, value):
    b = Branch.model_validate(edge_branch(t, value))
    assert b.is_edge and "exactly at the limit (total_days_12m = " in b.targets[2].text()


@pytest.mark.parametrize("t, value, message", [
    (M, 29, "does not meet"),            # 29 fails >= 30
    (F, 30, "does not fail"),
    (M, 45, "not at the limit"),          # meets, but far from the limit
    (F, 10, "not at the limit"),
])
def test_wrong_edge_values(t, value, message):
    with pytest.raises(ValidationError, match=message):
        Branch.model_validate(edge_branch(t, value))


def test_edge_needs_a_value_a_number_fact_and_one_pin():
    with pytest.raises(ValidationError, match="needs its edge_value"):
        Branch.model_validate(edge_branch(M, 30, edge_value=None))
    data = edge_branch(M, 30)
    data["targets"][0] |= {"at_edge": True, "edge_value": 1}
    with pytest.raises(ValidationError, match="only number conditions"):
        Branch.model_validate(data)
    data = branch(1, (M,) * 5)
    data["targets"][2]["edge_value"] = 30
    with pytest.raises(ValidationError, match="only for edge targets"):
        Branch.model_validate(data)


# --- a branch set --------------------------------------------------------------------------------------------

def test_complete_set():
    s = BranchSet.model_validate(full_set())
    assert len(s.branches) == 32
    assert s.branches[0].pattern == "✔✔✔✔✔" and s.branches[-1].pattern == "✘✘✘✘✘"
    assert sum(b.all_true for b in s.branches) == 1
    assert s.summary() == "REQ-41-PRECONDITION v1: 1 reading(s) x 2^5 = 32 branches (32 possible, 0 impossible) + 0 edge branch(es)"


def test_or_set():
    s = BranchSet.model_validate(full_set("OR"))
    assert sum(b.all_true for b in s.branches) == 31


def test_missing_branch_is_refused():
    data = full_set()
    del data["branches"][7]
    with pytest.raises(ValidationError, match="31 distinct combinations, need all 32"):
        BranchSet.model_validate(data)


def test_duplicate_combination_is_refused():
    data = full_set()
    data["branches"][7] = branch(8, patterns()[6])
    with pytest.raises(ValidationError, match="need all 32"):
        BranchSet.model_validate(data)


def test_duplicate_id_and_bad_numbering():
    data = full_set()
    data["branches"][1]["branch_id"] = data["branches"][0]["branch_id"]
    with pytest.raises(ValidationError, match="appears twice"):
        BranchSet.model_validate(data)
    data = full_set()
    data["branches"][1]["number"] = 99
    with pytest.raises(ValidationError, match="numbered 1..32"):
        BranchSet.model_validate(data)


def test_branches_must_come_from_the_same_card_version():
    data = full_set()
    data["branches"][3]["card_version"] = 2
    with pytest.raises(ValidationError, match="different card version"):
        BranchSet.model_validate(data)


def test_every_reading_needs_its_own_full_set():
    other = {"C3": "p.136 reading"}
    data = full_set(readings=[{}, other])
    with pytest.raises(ValidationError, match="p.136 reading.*0 distinct combinations"):
        BranchSet.model_validate(data)
    data["branches"] += [branch(i, p, reading=other, prefix="-R2") for i, p in enumerate(patterns(), start=1)]
    assert len(BranchSet.model_validate(data).branches) == 64


def test_reading_not_in_the_set_is_refused():
    data = full_set()
    data["branches"][0]["reading"] = {"C3": "unknown"}
    with pytest.raises(ValidationError, match="reading that is not in the set"):
        BranchSet.model_validate(data)


def test_edge_branches_are_extra_and_must_point_to_a_branch():
    data = full_set()
    data["branches"].append(edge_branch(M, 30))
    s = BranchSet.model_validate(data)
    assert "+ 1 edge branch(es)" in s.summary()
    data["branches"][-1]["edge_of"] = f"{CARD.id}-B99"
    with pytest.raises(ValidationError, match="points to unknown branch"):
        BranchSet.model_validate(data)


def test_impossible_branches_still_count_towards_2_to_the_n():
    data = full_set()
    data["branches"][-1] |= {"status": "impossible", "reason": "test"}
    assert "(31 possible, 1 impossible)" in BranchSet.model_validate(data).summary()
