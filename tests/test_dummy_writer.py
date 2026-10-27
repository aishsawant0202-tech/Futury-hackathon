"""Writing the SQL dummy file: only after a passed self-check, complete, verified, never half-written."""
import datetime as dt
import sqlite3

import pytest

from specpilot.dummies import build
from specpilot.review import approve
from specpilot.tools import dummy_writer
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import WriteRefused, load, write_sqlite
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import make_people
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def card():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def branches(card):
    return make_branches(card, edges=True)


@pytest.fixture(scope="module")
def dummy(branches):
    return build(make_people(branches, 4711), branches, "test")


@pytest.fixture(scope="module")
def passed(card, branches, dummy):
    report = check(card, branches, dummy)
    assert report.ok
    return report


def rows(path, sql):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


# --- a correct write ------------------------------------------------------------------------------------

def test_file_name_and_nothing_else_left(dummy, passed, tmp_path):
    path = write_sqlite(dummy, passed, tmp_path)
    assert path.name == "REQ-41-PRECONDITION-v1-seed4711.sqlite"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_tables_and_row_counts(dummy, passed, tmp_path):
    path = write_sqlite(dummy, passed, tmp_path)
    counts = {t: rows(path, f"SELECT COUNT(*) FROM {t}")[0][0]
              for t in ("dummy_people", "dummy_previous_periods", "dummy_coverage", "dummy_meta")}
    assert counts == {"dummy_people": 34, "dummy_previous_periods": len(dummy.previous), "dummy_coverage": 34,
                      "dummy_meta": 11}


def test_columns(dummy, passed, tmp_path):
    path = write_sqlite(dummy, passed, tmp_path)
    cols = [r[1] for r in rows(path, "PRAGMA table_info(dummy_people)")]
    assert cols[:2] == ["person_id", "neutral_id"]
    assert {"report_date", "current_start", "current_end", "current_certified", "open_end", "statutory_insured",
            "marginal_employment", "C1_truth", "C5_truth", "all_true", "edge_criterion", "edge_value",
            "provenance", "seed", "card_hash"} <= set(cols)


def test_values_as_written(dummy, passed, tmp_path):
    path = write_sqlite(dummy, passed, tmp_path)
    p = dummy.people[1]  # B01-E1: the 30-day edge person
    row = rows(path, f"SELECT neutral_id, current_start, statutory_insured, C3_truth, all_true, edge_criterion, "
                     f"edge_value, provenance FROM dummy_people WHERE person_id = '{p.person_id}'")[0]
    assert row == (p.neutral_id, p.current_start.isoformat(), int(p.statutory_insured), 1, 1, "C3", 30, "SYNTHETIC")


def test_meta_records_origin_and_self_check(dummy, passed, tmp_path):
    meta = dict(rows(write_sqlite(dummy, passed, tmp_path), "SELECT key, value FROM dummy_meta"))
    assert meta["card_hash"] == dummy.card_hash and meta["seed"] == "4711" and meta["provenance"] == "SYNTHETIC"
    assert meta["self_check"].startswith("SELF-CHECK PASSED: 34 people")


def test_read_back_is_identical(dummy, passed, tmp_path):
    assert load(write_sqlite(dummy, passed, tmp_path)) == dummy


def test_same_input_same_file(dummy, passed, tmp_path):
    a = write_sqlite(dummy, passed, tmp_path / "a").read_bytes()
    b = write_sqlite(dummy, passed, tmp_path / "b").read_bytes()
    assert a == b


def test_rewriting_replaces_the_file_cleanly(dummy, passed, tmp_path):
    first = write_sqlite(dummy, passed, tmp_path).read_bytes()
    assert write_sqlite(dummy, passed, tmp_path).read_bytes() == first
    assert len(list(tmp_path.iterdir())) == 1


def test_database_rules_hold(dummy, passed, tmp_path):
    path = write_sqlite(dummy, passed, tmp_path)
    assert rows(path, "PRAGMA foreign_key_check") == [] and rows(path, "PRAGMA integrity_check") == [("ok",)]
    c = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE dummy_people SET provenance = 'REAL' WHERE neutral_id = 'DUMMY-0001'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE dummy_people SET C1_truth = 2 WHERE neutral_id = 'DUMMY-0001'")
    c.close()


def test_impossible_branches_are_written_to_the_coverage(tmp_path):
    from specpilot.criteria import CriteriaCard
    from tests.test_branch_maker import custom
    c = custom(("prior_certified_6m", "=", True), ("total_days_12m", "<=", 1))
    c = CriteriaCard.model_validate(c.model_dump() | {"status": "approved", "reviewer": "A",
                                                      "reviewed_at": dt.datetime(2027, 1, 1), "approved_hash": c.content_hash()})
    s = make_branches(c)
    d = build(make_people(s, 1), s, "test")
    path = write_sqlite(d, check(c, s, d), tmp_path)
    cov = rows(path, "SELECT outcome, person_id, reason FROM dummy_coverage ORDER BY position")
    assert [o for o, _, _ in cov].count("impossible") == 1
    imp = next(r for r in cov if r[0] == "impossible")
    assert imp[1] is None and "proved" in imp[2]
    assert "proven impossible" in dict(rows(path, "SELECT key, value FROM dummy_meta"))["notes"]


# --- refusals --------------------------------------------------------------------------------------------------

def test_failed_self_check_writes_nothing(card, branches, dummy, tmp_path):
    people = list(dummy.people)
    people[0] = people[0].model_copy(update={"statutory_insured": False})
    bad = dummy.model_copy(update={"people": people})
    report = check(card, branches, bad)
    with pytest.raises(WriteRefused, match="1 problem|2 problem"):
        write_sqlite(bad, report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_report_for_other_data_is_refused(dummy, passed, tmp_path):
    fewer = dummy.model_copy(update={"people": dummy.people[:-1]})
    with pytest.raises(WriteRefused, match="covered 34 people, the file has 33"):
        write_sqlite(fewer, passed, tmp_path)


def test_crash_while_writing_leaves_no_file(dummy, passed, tmp_path, monkeypatch):
    def crash(path, d, report):
        path.write_bytes(b"half a file")
        raise OSError("disk full")

    monkeypatch.setattr(dummy_writer, "_write", crash)
    with pytest.raises(OSError, match="disk full"):
        write_sqlite(dummy, passed, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_crash_keeps_an_earlier_good_file(dummy, passed, tmp_path, monkeypatch):
    good = write_sqlite(dummy, passed, tmp_path).read_bytes()
    monkeypatch.setattr(dummy_writer, "_write", lambda path, d, r: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_sqlite(dummy, passed, tmp_path)
    assert [p.read_bytes() for p in tmp_path.iterdir()] == [good]


def test_read_back_mismatch_writes_nothing(dummy, passed, tmp_path, monkeypatch):
    monkeypatch.setattr(dummy_writer, "load", lambda path: dummy.model_copy(update={"seed": 1}))
    with pytest.raises(WriteRefused, match="read back differs"):
        write_sqlite(dummy, passed, tmp_path)
    assert list(tmp_path.iterdir()) == []
