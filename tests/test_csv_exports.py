"""CSV exports: the full file loses nothing, the stripped file reveals nothing, all three files are one unit."""
import csv
import datetime as dt
import io

import pytest

from specpilot.dummies import build
from specpilot.models import AbsencePeriod, Scenario
from specpilot.review import approve
from specpilot.tools import dummy_writer
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dummy_writer import STRIPPED_COLUMNS, WriteRefused, load, write_outputs
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
    return check(card, branches, dummy)


@pytest.fixture
def out(dummy, passed, tmp_path):
    return write_outputs(dummy, passed, tmp_path)


def read(path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="ascii"))))


# --- the three files ---------------------------------------------------------------------------------------

def test_three_files_and_nothing_else(out, tmp_path):
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "REQ-41-PRECONDITION-v1-seed4711.full.csv", "REQ-41-PRECONDITION-v1-seed4711.sqlite",
        "REQ-41-PRECONDITION-v1-seed4711.stripped.csv"]


def test_sql_file_is_the_same_as_before(out, dummy):
    assert load(out.sqlite) == dummy


def test_same_input_same_files(dummy, passed, tmp_path):
    a = write_outputs(dummy, passed, tmp_path / "a")
    b = write_outputs(dummy, passed, tmp_path / "b")
    for x, y in ((a.sqlite, b.sqlite), (a.full_csv, b.full_csv), (a.stripped_csv, b.stripped_csv)):
        assert x.read_bytes() == y.read_bytes()


# --- the full CSV loses nothing -----------------------------------------------------------------------------

def test_full_csv_one_row_per_person_in_branch_order(out, dummy):
    rows = read(out.full_csv)
    assert [r["person_id"] for r in rows] == [p.person_id for p in dummy.people]


def test_full_csv_rebuilds_every_person_exactly(out, dummy):
    """Nothing is lost: each row gives back the identical person, labels included."""
    def day(s):
        return dt.date.fromisoformat(s)

    for r, p in zip(read(out.full_csv), dummy.people):
        n = int(r["n_previous"])
        rebuilt = Scenario(report_date=day(r["report_date"]),
                           current=AbsencePeriod(start=day(r["current_start"]), end=day(r["current_end"]),
                                                 certified=r["current_certified"] == "1"),
                           previous=[AbsencePeriod(start=day(r[f"prev{i}_start"]), end=day(r[f"prev{i}_end"]),
                                                   certified=r[f"prev{i}_certified"] == "1") for i in range(1, n + 1)],
                           statutory_insured=r["statutory_insured"] == "1",
                           marginal_employment=r["marginal_employment"] == "1", open_end=r["open_end"] == "1")
        assert rebuilt == dummy.scenario_of(p)
        assert {k: int(r[f"{k}_truth"]) for k in p.truth} == p.truth and int(r["all_true"]) == p.all_true
        assert r["edge_value"] == ("" if p.edge_value is None else str(p.edge_value))
        assert all(r[f"prev{i}_start"] == "" for i in range(n + 1, 5))


# --- the stripped CSV reveals nothing ---------------------------------------------------------------------------

def test_stripped_csv_has_only_raw_values(out):
    rows = read(out.stripped_csv)
    assert list(rows[0]) == STRIPPED_COLUMNS
    assert not {"person_id", "branch_id", "all_true", "edge_value", "seed", "card_hash"} & set(rows[0])
    assert not any(c.endswith("_truth") for c in rows[0])
    assert all(r["provenance"] == "SYNTHETIC" for r in rows)


def test_stripped_csv_contains_no_branch_or_person_ids(out, dummy):
    text = out.stripped_csv.read_text(encoding="ascii")
    assert not any(p.branch_id in text or p.person_id in text for p in dummy.people)


def test_stripped_csv_is_sorted_by_neutral_id_and_order_says_nothing(out, dummy):
    rows = read(out.stripped_csv)
    assert [r["neutral_id"] for r in rows] == [f"DUMMY-{n:04d}" for n in range(1, 35)]
    designed = {p.neutral_id: p.all_true for p in dummy.people}
    assert designed["DUMMY-0001"] == 0 or designed["DUMMY-0002"] == 0  # the all-met people are not simply first


def test_stripped_rows_match_the_full_rows(out):
    full = {r["neutral_id"]: r for r in read(out.full_csv)}
    for r in read(out.stripped_csv):
        assert all(r[c] == full[r["neutral_id"]][c] for c in STRIPPED_COLUMNS)


def test_plain_ascii_and_unix_line_ends(out):
    for f in (out.full_csv, out.stripped_csv):
        raw = f.read_bytes()
        assert raw.isascii() and b"\r\n" not in raw


# --- one unit: all or nothing --------------------------------------------------------------------------------------

def test_failed_self_check_writes_no_file(card, branches, dummy, tmp_path):
    people = list(dummy.people)
    people[0] = people[0].model_copy(update={"statutory_insured": not people[0].statutory_insured})
    bad = dummy.model_copy(update={"people": people})
    report = check(card, branches, bad)
    assert not report.ok
    with pytest.raises(WriteRefused):
        write_outputs(bad, report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_crash_while_writing_the_csvs_leaves_no_file_at_all(dummy, passed, tmp_path, monkeypatch):
    monkeypatch.setattr(dummy_writer, "csv_text", lambda d, stripped: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_outputs(dummy, passed, tmp_path)
    assert list(tmp_path.iterdir()) == []  # not even the SQL file


def test_a_leaking_stripped_file_is_refused(dummy, passed, tmp_path, monkeypatch):
    real = dummy_writer.csv_text
    monkeypatch.setattr(dummy_writer, "csv_text",
                        lambda d, stripped: real(d, False) if stripped else real(d, False))
    with pytest.raises(WriteRefused):
        write_outputs(dummy, passed, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_stripped_check_catches_a_branch_id_in_a_value(dummy, passed, tmp_path, monkeypatch):
    real = dummy_writer.csv_text
    monkeypatch.setattr(dummy_writer, "csv_text", lambda d, stripped: real(d, stripped).replace(
        "SYNTHETIC", dummy.people[0].branch_id, 1) if stripped else real(d, stripped))
    with pytest.raises(WriteRefused, match="reveal the design"):
        write_outputs(dummy, passed, tmp_path)
