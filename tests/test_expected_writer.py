"""Writing the answers: only after a passed consistency check, complete, verified, never half-written."""
import csv
import io
import sqlite3

import pytest

from specpilot.dummies import build
from specpilot.review import approve
from specpilot.tools import expected_writer
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import load as load_dummy, write_outputs
from specpilot.tools.expected_results import check_consistency, compute
from specpilot.tools.expected_writer import WriteRefused, load, write_expected
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def dummy_path(card, tmp_path_factory):
    s = make_branches(card, edges=True)
    d = build(make_people(s, 4711), s, "test")
    return write_outputs(d, check(card, s, d), tmp_path_factory.mktemp("dummies")).sqlite


@pytest.fixture(scope="module")
def expected(card, dummy_path):
    return compute(card, dummy_path)


@pytest.fixture(scope="module")
def consistent(expected, dummy_path):
    c = check_consistency(expected, load_dummy(dummy_path))
    assert c.ok
    return c


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --- a correct write ------------------------------------------------------------------------------------------

def test_two_files_named_after_the_dummy_file(expected, consistent, tmp_path):
    out = write_expected(expected, consistent, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["REQ-41-PRECONDITION-v1-seed4711.expected.csv",
                                                           "REQ-41-PRECONDITION-v1-seed4711.expected.sqlite"]
    assert out.sqlite.name.startswith(expected.dummy_file.removesuffix(".sqlite"))


def test_default_folder_is_separate_from_the_dummy_files():
    from specpilot.tools import dummy_writer
    assert expected_writer.OUT_DIR != dummy_writer.OUT_DIR
    assert expected_writer.OUT_DIR.name == "expected"


def test_tables(expected, consistent, tmp_path):
    out = write_expected(expected, consistent, tmp_path)
    assert rows(out.sqlite, "SELECT COUNT(*) FROM expected_results") == [(34,)]
    assert rows(out.sqlite, "SELECT COUNT(*) FROM expected_conditions") == [(34 * 5,)]
    assert rows(out.sqlite, "SELECT SUM(message_expected) FROM expected_results") == [(2,)]
    assert rows(out.sqlite, "PRAGMA foreign_key_check") == []


def test_meta_ties_the_answers_to_their_dummy_file(expected, consistent, dummy_path, tmp_path):
    from specpilot.tools.expected_results import sha256
    meta = dict(rows(write_expected(expected, consistent, tmp_path).sqlite, "SELECT key, value FROM expected_meta"))
    assert meta["dummy_file"] == dummy_path.name and meta["dummy_sha256"] == sha256(dummy_path)
    assert meta["card_hash"] == expected.card_hash and meta["seed"] == "4711"
    assert meta["consistency"].startswith("CONSISTENCY PASSED: 34 answer(s)")


def test_conditions_keep_rule_and_quote(expected, consistent, tmp_path):
    out = write_expected(expected, consistent, tmp_path)
    rule_ids, quote = rows(out.sqlite, "SELECT rule_ids, quote FROM expected_conditions WHERE criterion_id = 'C3' "
                                       "LIMIT 1")[0]
    assert rule_ids == '["R-3.12-003"]' and "mindestens 30 Tage" in quote


def test_read_back_is_identical(expected, consistent, tmp_path):
    assert load(write_expected(expected, consistent, tmp_path).sqlite) == expected


def test_csv_one_row_per_answer(expected, consistent, tmp_path):
    out = write_expected(expected, consistent, tmp_path)
    data = list(csv.DictReader(io.StringIO(out.csv.read_text(encoding="ascii"))))
    assert [r["person_id"] for r in data] == [r.person_id for r in expected.results]
    edge = next(r for r in data if r["person_id"].endswith("B05-E1-P1"))
    assert (edge["message_expected"], edge["C3_condition"], edge["C3_value"], edge["C3_met"]) == (
        "0", "total_days_12m >= 30", "29", "0")
    assert out.csv.read_bytes().isascii()


def test_same_input_same_files(expected, consistent, tmp_path):
    a = write_expected(expected, consistent, tmp_path / "a")
    b = write_expected(expected, consistent, tmp_path / "b")
    assert a.sqlite.read_bytes() == b.sqlite.read_bytes() and a.csv.read_bytes() == b.csv.read_bytes()


# --- refusals ----------------------------------------------------------------------------------------------------

def test_failed_consistency_writes_nothing(card, dummy_path, tmp_path):
    import shutil
    bad = shutil.copy(dummy_path, tmp_path / dummy_path.name)
    conn = sqlite3.connect(bad)
    with conn:
        conn.execute("UPDATE dummy_people SET statutory_insured = 0 WHERE person_id = 'REQ-41-PRECONDITION-B01-P1'")
    conn.close()
    e = compute(card, bad)
    c = check_consistency(e, load_dummy(bad))
    with pytest.raises(WriteRefused, match="2 problem"):
        write_expected(e, c, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_check_that_covered_too_few_people_is_refused(expected, consistent, tmp_path):
    from dataclasses import replace
    with pytest.raises(WriteRefused, match="compared 33 answer"):
        write_expected(expected, replace(consistent, compared=33), tmp_path)


def test_crash_while_writing_leaves_nothing(expected, consistent, tmp_path, monkeypatch):
    monkeypatch.setattr(expected_writer, "csv_text", lambda e: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_expected(expected, consistent, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_read_back_mismatch_writes_nothing(expected, consistent, tmp_path, monkeypatch):
    monkeypatch.setattr(expected_writer, "load", lambda path: expected.model_copy(update={"seed": 1}))
    with pytest.raises(WriteRefused, match="read back differs"):
        write_expected(expected, consistent, tmp_path)
    assert list(tmp_path.iterdir()) == []
