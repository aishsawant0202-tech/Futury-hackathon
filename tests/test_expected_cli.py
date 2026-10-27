"""The expected-results command: dummy file + approved card -> checked answers in their own folder."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from specpilot.dummies import build
from specpilot.review import approve, save_card
from specpilot.tools import expected_results
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import write_outputs
from specpilot.tools.expected_writer import load
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def made(card, tmp_path_factory):
    folder = tmp_path_factory.mktemp("made")
    s = make_branches(card, edges=True)
    d = build(make_people(s, 4711), s, "test")
    dummy = write_outputs(d, check(card, s, d), folder / "dummies").sqlite
    save_card(card, folder / "card.json")
    return dummy, folder / "card.json"


@pytest.fixture
def files(made, tmp_path):
    """Private copies a test may damage."""
    dummy, card_file = made
    (tmp_path / "dummies").mkdir()
    return (Path(shutil.copy(dummy, tmp_path / "dummies" / dummy.name)),
            Path(shutil.copy(card_file, tmp_path / "card.json")))


def run(argv, capsys):
    code = expected_results.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- writing ------------------------------------------------------------------------------------------------------

def test_writes_the_answers(files, tmp_path, capsys):
    code, out, _ = run([*files, "--out", tmp_path / "expected"], capsys)
    assert code == 0
    assert "34 expected results; message must be sent in 2, must not be sent in 32" in out
    assert "CONSISTENCY PASSED: 34 answer(s)" in out
    names = sorted(p.name for p in (tmp_path / "expected").iterdir())
    assert names == ["REQ-41-PRECONDITION-v1-seed4711.expected.csv", "REQ-41-PRECONDITION-v1-seed4711.expected.sqlite"]
    assert len(load(tmp_path / "expected" / names[1]).results) == 34


def test_dry_run_writes_nothing(files, tmp_path, capsys):
    code, out, _ = run([*files, "--dry-run", "--out", tmp_path / "expected"], capsys)
    assert code == 0 and "Dry run passed: nothing was written." in out
    assert not (tmp_path / "expected").exists()


def test_explain_one_person(files, capsys):
    code, out, _ = run([*files, "--dry-run", "--explain", "DUMMY-0001"], capsys)
    assert code == 0 and "DUMMY-0001 (REQ-41-PRECONDITION-" in out and "C3 " in out and "R-3.12-003" in out
    code, _, err = run([*files, "--dry-run", "--explain", "DUMMY-9999"], capsys)
    assert code == 1 and "no person DUMMY-9999" in err


# --- the answers stay away from the dummy file ---------------------------------------------------------------------

def test_refuses_to_write_into_the_dummy_folder(files, capsys):
    dummy, _ = files
    code, _, err = run([*files, "--out", dummy.parent], capsys)
    assert code == 1 and "must not be written into the dummy file's folder" in err
    assert sorted(p.name for p in dummy.parent.iterdir()) == [dummy.name]


# --- refusals ---------------------------------------------------------------------------------------------------------

def test_draft_card_is_refused(files, tmp_path, capsys):
    save_card(draft(), tmp_path / "draft.json")
    code, _, err = run([files[0], tmp_path / "draft.json", "--out", tmp_path / "expected"], capsys)
    assert code == 1 and "not approved" in err and not (tmp_path / "expected").exists()


def test_missing_files(files, tmp_path, capsys):
    code, _, err = run([tmp_path / "nope.sqlite", files[1]], capsys)
    assert code == 1 and "not found" in err
    code, _, err = run([files[0], tmp_path / "nope.json"], capsys)
    assert code == 1 and "cannot read" in err


def test_inconsistent_dummy_file_writes_nothing(files, tmp_path, capsys):
    dummy, _ = files
    conn = sqlite3.connect(dummy)
    with conn:
        conn.execute("UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = 'REQ-41-PRECONDITION-B01-P1'")
    conn.close()
    code, out, err = run([*files, "--out", tmp_path / "expected"], capsys)
    assert code == 1 and "CONSISTENCY FAILED" in out and "nothing was written" in err
    assert not (tmp_path / "expected").exists()


def test_inconsistent_dummy_file_fails_a_dry_run_too(files, capsys):
    dummy, _ = files
    conn = sqlite3.connect(dummy)
    with conn:
        conn.execute("UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = 'REQ-41-PRECONDITION-B01-P1'")
    conn.close()
    code, out, _ = run([*files, "--dry-run"], capsys)
    assert code == 1 and "Dry run passed" not in out
