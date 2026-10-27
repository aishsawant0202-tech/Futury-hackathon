"""Value finder batch + command: every branch accounted for once, a preview only, reproducible from the seed."""
import pytest
import z3
from pydantic import ValidationError

from specpilot.tools import gold, value_finder
from specpilot.tools.branch_maker import make_branches, write_branches
from specpilot.tools.value_finder import InputError, PeopleSet, check_person, load_branches, make_people
from tests.test_branch_maker import custom

SET = make_branches(gold.load("REQ-41-PRECONDITION"), edges=True)
SEED = 4711


@pytest.fixture(scope="module")
def people():
    return make_people(SET, SEED)


@pytest.fixture
def branches_file(tmp_path):
    json_path, _ = write_branches(SET, tmp_path)
    return json_path


def run(argv, capsys):
    code = value_finder.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- the people set -----------------------------------------------------------------------------------

def test_one_person_per_branch(people):
    assert len(people.people) == 34 and not people.impossible and not people.unsolved
    assert [p.branch_id for p in people.people] == [b.branch_id for b in SET.branches]
    assert people.people[0].person_id == "REQ-41-PRECONDITION-B01-P1"
    assert people.people[1].person_id == "REQ-41-PRECONDITION-B01-E1-P1"
    assert (people.card_id, people.card_version, people.card_hash, people.seed) == (
        SET.card_id, SET.card_version, SET.card_hash, SEED)


def test_every_person_reaches_its_branch(people):
    by_id = {b.branch_id: b for b in SET.branches}
    assert all(check_person(by_id[p.branch_id], p.scenario) == [] for p in people.people)


def test_impossible_branches_are_listed_not_dropped():
    s = make_branches(custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1)))
    ps = make_people(s, SEED)
    assert len(ps.people) + len(ps.impossible) == 4 and len(ps.impossible) == 1
    assert "proved" in ps.impossible[0].reason
    assert "1 impossible" in ps.summary()


def test_round_trip(people):
    assert PeopleSet.model_validate_json(people.model_dump_json()) == people


@pytest.mark.parametrize("change, message", [
    (lambda d: d["people"].pop(3), "missing"),
    (lambda d: d["people"].append(d["people"][0]), "twice or unknown"),
    (lambda d: d["impossible"].append({"branch_id": d["people"][0]["branch_id"], "reason": "test reason"}), "twice"),
    (lambda d: d["people"][2].update(person_id="REQ-41-PRECONDITION-B99-P1"), "does not belong"),
    (lambda d: d["people"][2].update(person_id="REQ-41-PRECONDITION-B01-P1"), "appears twice"),
    (lambda d: d["people"][2].update(person_id="REQ-41-PRECONDITION-B01-E1-P2"), "does not belong"),
])
def test_incomplete_or_inconsistent_sets_are_refused(people, change, message):
    data = people.model_dump(mode="json")
    change(data)
    with pytest.raises(ValidationError, match=message):
        PeopleSet.model_validate(data)


# --- reading branches files --------------------------------------------------------------------------------

def test_load_branches(branches_file, tmp_path):
    assert load_branches(branches_file) == SET
    with pytest.raises(InputError, match="cannot read"):
        load_branches(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text('{"card_id": "x"}', encoding="utf-8")
    with pytest.raises(InputError, match="not a valid branches file"):
        load_branches(tmp_path / "bad.json")


def test_tampered_branches_file_is_refused(branches_file):
    """Removing a branch from the file breaks the 2^n guarantee: refused."""
    import json
    data = json.loads(branches_file.read_text(encoding="utf-8"))
    data["branches"].pop(4)
    branches_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InputError, match="need all 32"):
        load_branches(branches_file)


# --- command line --------------------------------------------------------------------------------------------

def test_cli_preview_writes_nothing(branches_file, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    code, out, _ = run([branches_file, "--seed", SEED, "--limit", 3], capsys)
    assert code == 0
    assert "PREVIEW: nothing is written. Seed 4711" in out and "34 people, 0 impossible, 0 unsolved" in out
    assert "REQ-41-PRECONDITION-B01-P1" in out and "31 more (--limit 34 shows all)" in out
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_cli_same_seed_same_screen(branches_file, capsys):
    a = run([branches_file, "--seed", 7, "--limit", 34], capsys)[1]
    b = run([branches_file, "--seed", 7, "--limit", 34], capsys)[1]
    assert a == b


def test_cli_without_seed_shows_one_to_repeat_with(branches_file, capsys, monkeypatch):
    monkeypatch.setattr(value_finder.secrets, "randbelow", lambda n: 123)
    code, out, _ = run([branches_file, "--limit", 1], capsys)
    assert code == 0 and "Seed 123 (repeat with --seed 123)" in out


def test_cli_bad_file(tmp_path, capsys):
    code, _, err = run([tmp_path / "nope.json"], capsys)
    assert code == 1 and "cannot read" in err


def test_cli_unsolved_is_an_error(branches_file, capsys, monkeypatch):
    monkeypatch.setattr(z3.Solver, "check", lambda self, *a: z3.unknown)
    code, out, err = run([branches_file, "--seed", 1, "--limit", 0], capsys)
    assert code == 1 and "34 unsolved" in out and "branch(es) unsolved" in err
    assert "UNSOLVED (problem, not a proof)" in out
