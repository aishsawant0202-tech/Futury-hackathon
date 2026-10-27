"""The final command: approved card + branches + seed -> checked dummy files, or a clear refusal."""
import pytest

from specpilot.criteria import CriteriaCard
from specpilot.review import approve, save_card
from specpilot.tools import dummy_writer
from specpilot.tools.branch_maker import make_branches, write_branches
from specpilot.tools.dummy_writer import load
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture
def files(card, tmp_path):
    save_card(card, tmp_path / "card.json")
    branches, _ = write_branches(make_branches(card, edges=True), tmp_path)
    return tmp_path / "card.json", branches


def run(argv, capsys):
    code = dummy_writer.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- writing -------------------------------------------------------------------------------------------------

def test_writes_the_three_files(files, tmp_path, capsys):
    code, out, _ = run([*files, "--seed", 4711, "--out", tmp_path / "dummies"], capsys)
    assert code == 0
    assert "SELF-CHECK PASSED: 34 people checked against 34 branches" in out
    assert "32 branches + 2 edge branch(es) -> 34 people, 0 impossible, 0 unsolved · seed 4711" in out
    assert sorted(p.name for p in (tmp_path / "dummies").iterdir()) == [
        "REQ-41-PRECONDITION-v1-seed4711.full.csv", "REQ-41-PRECONDITION-v1-seed4711.sqlite",
        "REQ-41-PRECONDITION-v1-seed4711.stripped.csv"]
    assert len(load(tmp_path / "dummies" / "REQ-41-PRECONDITION-v1-seed4711.sqlite").people) == 34


def test_dry_run_writes_nothing(files, tmp_path, capsys):
    code, out, _ = run([*files, "--seed", 4711, "--dry-run", "--out", tmp_path / "dummies"], capsys)
    assert code == 0 and "DRY RUN" in out and "Dry run passed: nothing was written." in out
    assert not (tmp_path / "dummies").exists()


def test_same_seed_same_files(files, tmp_path, capsys):
    run([*files, "--seed", 9, "--out", tmp_path / "a"], capsys)
    run([*files, "--seed", 9, "--out", tmp_path / "b"], capsys)
    for name in ("sqlite", "full.csv", "stripped.csv"):
        f = f"REQ-41-PRECONDITION-v1-seed9.{name}"
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_without_seed_a_random_one_is_shown(files, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dummy_writer.secrets, "randbelow", lambda n: 321)
    code, out, _ = run([*files, "--dry-run"], capsys)
    assert code == 0 and "Seed 321 (repeat with --seed 321)" in out


# --- refusals --------------------------------------------------------------------------------------------------

def test_draft_card_is_refused(card, files, tmp_path, capsys):
    save_card(draft(), tmp_path / "draft.json")
    code, _, err = run([tmp_path / "draft.json", files[1], "--seed", 1, "--out", tmp_path / "d"], capsys)
    assert code == 1 and "not approved" in err and not (tmp_path / "d").exists()


def test_branches_of_another_card_version_are_refused_before_the_slow_part(card, files, tmp_path, capsys, monkeypatch):
    v2 = approve(CriteriaCard.model_validate(card.model_dump() | {
        "version": 2, "status": "draft", "reviewer": "", "reviewed_at": None, "approved_hash": ""}), "A", RULES)
    save_card(v2, tmp_path / "v2.json")
    monkeypatch.setattr("specpilot.tools.value_finder.make_people",
                        lambda *a, **k: pytest.fail("must stop before making people"))
    code, _, err = run([tmp_path / "v2.json", files[1], "--seed", 1], capsys)
    assert code == 1 and "not from the approved REQ-41-PRECONDITION v2" in err


def test_missing_files(files, tmp_path, capsys):
    assert run([tmp_path / "nope.json", files[1]], capsys)[0] == 1
    code, _, err = run([files[0], tmp_path / "nope.json"], capsys)
    assert code == 1 and "cannot read" in err


def test_failed_self_check_writes_nothing(files, tmp_path, capsys, monkeypatch):
    """Corrupt one person between Station 4 and the check: the command must refuse to write."""
    import specpilot.dummies as dummies
    real = dummies.build

    def corrupt(*a, **k):
        d = real(*a, **k)
        people = list(d.people)
        people[0] = people[0].model_copy(update={"statutory_insured": not people[0].statutory_insured})
        return d.model_copy(update={"people": people})

    monkeypatch.setattr(dummies, "build", corrupt)
    code, out, err = run([*files, "--seed", 4711, "--out", tmp_path / "dummies"], capsys)
    assert code == 1 and "SELF-CHECK FAILED" in out and "nothing was written" in err
    assert not (tmp_path / "dummies").exists()


def test_dry_run_with_a_failed_self_check_is_an_error(files, tmp_path, capsys, monkeypatch):
    """A dry run must not report success when the check fails (the writer is never reached on a dry run)."""
    import specpilot.dummies as dummies
    real = dummies.build

    def corrupt(*a, **k):
        d = real(*a, **k)
        people = list(d.people)
        people[0] = people[0].model_copy(update={"statutory_insured": not people[0].statutory_insured})
        return d.model_copy(update={"people": people})

    monkeypatch.setattr(dummies, "build", corrupt)
    code, out, _ = run([*files, "--seed", 4711, "--dry-run"], capsys)
    assert code == 1 and "SELF-CHECK FAILED" in out and "Dry run passed" not in out
