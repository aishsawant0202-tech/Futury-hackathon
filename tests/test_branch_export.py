"""Branch maker input / output: only approved cards are exported; files are complete and reproducible."""
import csv
import json

import pytest

from specpilot.branches import BranchSet
from specpilot.criteria import CriteriaCard
from specpilot.review import approve, reject, save_card
from specpilot.tools import branch_maker
from specpilot.tools.branch_maker import BranchError, load_card, make_branches, write_branches
from tests.test_branch_maker import card as sized_card
from tests.test_review import RULES, draft


@pytest.fixture
def approved(tmp_path):
    path = tmp_path / "card.json"
    save_card(approve(draft(), "Examiner A", RULES), path)
    return path


def run(argv, capsys):
    code = branch_maker.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- which cards may be branched ---------------------------------------------------------------

def test_approved_card_loads(approved):
    assert load_card(approved).approval_valid


def test_draft_and_rejected_cards_are_refused(tmp_path):
    save_card(draft(), tmp_path / "d.json")
    with pytest.raises(BranchError, match="is draft, not approved"):
        load_card(tmp_path / "d.json")
    save_card(reject(draft(), "A", "wrong"), tmp_path / "r.json")
    with pytest.raises(BranchError, match="is rejected, not approved"):
        load_card(tmp_path / "r.json")


def test_card_changed_after_approval_is_refused(approved):
    data = json.loads(approved.read_text(encoding="utf-8"))
    data["criteria"][2]["value"] = 31
    approved.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BranchError, match="changed after it was approved"):
        load_card(approved)


def test_placeholder_rule_ids_are_refused(tmp_path):
    from specpilot.tools import gold
    g = gold.load("REQ-41-PRECONDITION")
    fake = CriteriaCard.model_validate(g.model_dump() | {"status": "approved", "reviewer": "x",
                                                         "reviewed_at": "2026-10-27T12:00:00", "approved_hash": g.content_hash()})
    save_card(fake, tmp_path / "g.json")
    with pytest.raises(BranchError, match="placeholder rule ids"):
        load_card(tmp_path / "g.json")


def test_missing_or_broken_files(tmp_path):
    with pytest.raises(BranchError, match="cannot read"):
        load_card(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text('{"id": "REQ-41-X"}', encoding="utf-8")
    with pytest.raises(BranchError, match="not a valid criteria card"):
        load_card(tmp_path / "bad.json")


def test_preview_accepts_drafts(tmp_path):
    save_card(draft(), tmp_path / "d.json")
    assert load_card(tmp_path / "d.json", preview=True).status.value == "draft"


# --- the files --------------------------------------------------------------------------------------

def test_json_file_is_the_full_branch_set(approved, tmp_path):
    s = make_branches(load_card(approved), edges=True)
    json_path, _ = write_branches(s, tmp_path / "out")
    assert json_path.name == "REQ-41-PRECONDITION-v1.branches.json"
    assert BranchSet.model_validate_json(json_path.read_text(encoding="utf-8")) == s


def test_csv_file_has_one_row_per_branch(approved, tmp_path):
    s = make_branches(load_card(approved), edges=True)
    _, csv_path = write_branches(s, tmp_path / "out")
    assert csv_path.name == "REQ-41-PRECONDITION-v1.branches.csv"
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 34
    assert rows[0]["pattern"] == "MMMMM" and rows[-1]["pattern"] == "FFFFF"
    e = next(r for r in rows if r["branch_id"] == "REQ-41-PRECONDITION-B05-E1")
    assert (e["C3_condition"], e["C3_target"], e["C3_edge_value"], e["edge_of"]) == (
        "total_days_12m >= 30", "failed", "29", "REQ-41-PRECONDITION-B05")
    assert rows[0]["free_facts"] == ";".join(s.branches[0].free_facts) and rows[0]["card_hash"] == s.card_hash
    assert rows[0]["free_facts"].startswith("current_open_end;")
    assert set(rows[0]) >= {"C1_condition", "C5_edge_value", "made_by", "reading", "reason"}


def test_impossible_reason_in_the_csv(tmp_path):
    from tests.test_branch_maker import custom
    s = make_branches(custom(("total_days_12m", ">=", 30), ("total_days_12m", "<=", 60)))
    _, csv_path = write_branches(s, tmp_path)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8", newline="")))
    assert rows[-1]["status"] == "impossible" and "does all of that" in rows[-1]["reason"]


def test_files_are_byte_identical_on_a_second_run(approved, tmp_path):
    a = write_branches(make_branches(load_card(approved), edges=True), tmp_path / "a")
    b = write_branches(make_branches(load_card(approved), edges=True), tmp_path / "b")
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]


# --- command line --------------------------------------------------------------------------------------

def test_cli_writes_the_files(approved, tmp_path, capsys):
    code, out, _ = run([approved, "--edges", "--out", tmp_path / "out"], capsys)
    assert code == 0
    assert "32 branches (32 possible, 0 impossible) + 2 edge branch(es)" in out
    assert "C3 pinned at 30" in out and "Free facts" in out
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "REQ-41-PRECONDITION-v1.branches.csv", "REQ-41-PRECONDITION-v1.branches.json"]


def test_cli_refuses_a_draft_and_previews_it_without_writing(tmp_path, capsys):
    save_card(draft(), tmp_path / "d.json")
    code, _, err = run([tmp_path / "d.json", "--out", tmp_path / "out"], capsys)
    assert code == 1 and "not approved" in err
    code, out, _ = run([tmp_path / "d.json", "--preview", "--out", tmp_path / "out"], capsys)
    assert code == 0 and "PREVIEW" in out and "nothing written" in out
    assert not (tmp_path / "out").exists()


def test_cli_size_limit_and_force(tmp_path, capsys):
    save_card(sized_card(12), tmp_path / "big.json")
    code, out, err = run([tmp_path / "big.json", "--preview"], capsys)
    assert code == 1 and "above the limit" in err and "2^12 = 4,096 branches per reading" in out
    code, out, _ = run([tmp_path / "big.json", "--preview", "--force", "--limit", "3"], capsys)
    assert code == 0 and "4093 more (--limit 4096 shows all)" in out

