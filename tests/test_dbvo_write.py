"""DBVO content checks and the content answers file."""
import csv
import io
import sqlite3

import pytest

from specpilot import content_review, dbvo
from specpilot.dbvo import ContentFile
from specpilot.review import approve
from specpilot.tools import dbvo_writer, facts
from specpilot.tools.dbvo_calc import check_content, content_file
from specpilot.tools.dbvo_writer import WriteRefused, load, write_content
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def content():
    return content_review.approve(dbvo.content_card(dbvo.load()), "Examiner A", accept_placeholders=True)


@pytest.fixture(scope="module")
def precondition():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def dummy_path(precondition, tmp_path_factory):
    from specpilot.dummies import build
    from specpilot.tools import scenarios
    from specpilot.tools.branch_maker import make_branches
    from specpilot.tools.dummy_writer import write_outputs
    from specpilot.tools.self_check import check
    from specpilot.tools.value_finder import make_people
    sc = approve(scenarios.build(precondition, RULES), "Examiner A", RULES)
    s = make_branches(sc, background=precondition)
    d = build(make_people(s, 4711), s, "test")
    return write_outputs(d, check(sc, s, d, precondition), tmp_path_factory.mktemp("content")).sqlite


@pytest.fixture(scope="module")
def cf(content, precondition, dummy_path):
    return content_file(content, precondition, dummy_path)


@pytest.fixture(scope="module")
def passed(content, precondition, dummy_path, cf):
    c = check_content(content, precondition, dummy_path, cf)
    assert c.ok
    return c


def people(dummy_path):
    from specpilot.tools.dummy_writer import load as load_dummy
    d = load_dummy(dummy_path)
    return d, {p.person_id: facts.evaluate(d.scenario_of(p)) for p in d.people}


def changed(cf, person_id, reading, **update):
    """The content file with one result changed, WITHOUT the form's checks (as if the calculator were wrong)."""
    results = [r.model_copy(update=update) if (r.person_id, r.reading) == (person_id, reading) else r for r in cf.results]
    return cf.model_copy(update={"results": results})


def problems(content, precondition, dummy_path, bad):
    return [(p.check, p.where) for p in check_content(content, precondition, dummy_path, bad).problems]


# --- correct content passes ----------------------------------------------------------------------------------------

def test_content_file_passes_its_checks(cf, passed):
    import re
    assert len(cf.results) == 24 and passed.compared == 24
    link, limit = map(int, re.match(r"the readings give different content through an exact 6-month link for (\d+) "
                                    r"people and the 12-month limit for (\d+) people", passed.notes[0]).groups())
    assert link == 4 and limit >= 1  # every exact-link person, and at least one person cut by the 12 months
    differ = int(re.search(r"differs between the readings for (\d+) people", cf.summary()).group(1))
    assert differ == link + limit  # every difference is accounted for by one of the two reasons


def test_level_1_dummy_file_passes_too(content, precondition, tmp_path):
    from specpilot.dummies import build
    from specpilot.tools.branch_maker import make_branches
    from specpilot.tools.dummy_writer import write_outputs
    from specpilot.tools.self_check import check
    from specpilot.tools.value_finder import make_people
    s = make_branches(precondition, edges=True)
    d = build(make_people(s, 4711), s, "test")
    path = write_outputs(d, check(precondition, s, d), tmp_path).sqlite
    f = content_file(content, precondition, path)
    assert check_content(content, precondition, path, f).ok
    assert sum(r.message_expected for r in f.results) == 2 * 2  # the 2 senders x 2 readings


# --- wrong content is caught --------------------------------------------------------------------------------------------

def an_exact_link_person(dummy_path):
    d, values = people(dummy_path)
    return next(pid for pid, v in values.items() if v["chain_link_exactly_6m"])


def test_reporting_past_the_exact_gap_under_p136(content, precondition, dummy_path, cf):
    pid = an_exact_link_person(dummy_path)
    p135 = next(r for r in cf.results if (r.person_id, r.reading) == (pid, "p.135"))
    bad = changed(cf, pid, "p.136", periods=p135.periods, fields=p135.fields)
    assert ("chain", f"{pid} p.136") in problems(content, precondition, dummy_path, bad)


def test_chain_stopped_too_early(content, precondition, dummy_path, cf):
    r = next(r for r in cf.results if len(r.periods) >= 2 and r.reading == "p.135")
    bad = changed(cf, r.person_id, r.reading, periods=r.periods[:-1])
    found = check_content(content, precondition, dummy_path, bad).problems
    assert any(p.check == "chain" and "stopped too early" in p.message for p in found)


def test_a_period_that_is_not_the_persons(content, precondition, dummy_path, cf):
    import datetime as dt
    r = next(r for r in cf.results if r.periods)
    fake = r.periods[0].model_copy(update={"start": r.periods[0].start - dt.timedelta(days=1)})
    bad = changed(cf, r.person_id, r.reading, periods=[fake, *r.periods[1:]])
    assert ("period", f"{r.person_id} {r.reading}") in problems(content, precondition, dummy_path, bad)


def test_wrong_order(content, precondition, dummy_path, cf):
    r = next(r for r in cf.results if len(r.periods) >= 2)
    flipped = [p.model_copy(update={"n": i}) for i, p in enumerate(reversed(r.periods), start=1)]
    bad = changed(cf, r.person_id, r.reading, periods=flipped)
    assert ("order", f"{r.person_id} {r.reading}") in problems(content, precondition, dummy_path, bad)


def test_skipped_list_must_match_the_absences_without_a_sick_note(content, precondition, dummy_path, cf):
    r = next(r for r in cf.results if r.skipped)
    bad = changed(cf, r.person_id, r.reading, skipped=[])
    assert ("skipped", f"{r.person_id} {r.reading}") in problems(content, precondition, dummy_path, bad)


def test_readings_may_only_differ_for_a_reason(content, precondition, dummy_path, cf):
    d, values = people(dummy_path)
    pid = next(p for p, v in values.items() if not (v["chain_link_exactly_6m"] or v["prior_ends_before_12m"]))
    r = next(r for r in cf.results if (r.person_id, r.reading) == (pid, "p.136"))
    bad = changed(cf, pid, "p.136", fields={**r.fields, "AU-AB-AG": "20270101"})
    assert ("readings", pid) in problems(content, precondition, dummy_path, bad)


def test_every_content_person_must_send(content, precondition, dummy_path, cf):
    r = cf.results[0]
    bad = changed(cf, r.person_id, r.reading, message_expected=False, fields={}, periods=[])
    assert ("message", f"{r.person_id} {r.reading}") in problems(content, precondition, dummy_path, bad)


# --- the content answers file ------------------------------------------------------------------------------------------------

def test_two_files_next_to_the_level_1_answers(cf, passed, tmp_path):
    out = write_content(cf, passed, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["REQ-41-CONTENT-SCENARIOS-v1-seed4711.dbvo.csv",
                                                           "REQ-41-CONTENT-SCENARIOS-v1-seed4711.dbvo.sqlite"]
    assert dbvo_writer.OUT_DIR.name == "expected"
    assert load(out.sqlite) == cf


def test_tables_and_meta(cf, passed, tmp_path):
    out = write_content(cf, passed, tmp_path)
    conn = sqlite3.connect(out.sqlite)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("dbvo_results", "dbvo_fields", "dbvo_periods", "dbvo_skipped")}
    meta = dict(conn.execute("SELECT key, value FROM dbvo_meta").fetchall())
    conn.close()
    assert counts["dbvo_results"] == 24 and counts["dbvo_periods"] == sum(len(r.periods) for r in cf.results)
    assert counts["dbvo_fields"] == sum(len(r.fields) for r in cf.results)
    assert meta["approved_with_placeholders"] == "14" and meta["dummy_sha256"] == cf.dummy_sha256
    assert meta["checks"].startswith("CONTENT CHECKS PASSED: 24 result(s)")


def test_csv_one_row_per_person_and_reading(cf, passed, tmp_path):
    out = write_content(cf, passed, tmp_path)
    rows = list(csv.DictReader(io.StringIO(out.csv.read_text(encoding="ascii"))))
    assert len(rows) == 24
    for row, r in zip(rows, cf.results):
        assert row["reading"] == r.reading and row["ANZAHL-AU"] == r.fields["ANZAHL-AU"]
        assert all(row[k] == v for k, v in r.fields.items())
    assert out.csv.read_bytes().isascii()


def test_same_input_same_files(cf, passed, tmp_path):
    a, b = write_content(cf, passed, tmp_path / "a"), write_content(cf, passed, tmp_path / "b")
    assert a.sqlite.read_bytes() == b.sqlite.read_bytes() and a.csv.read_bytes() == b.csv.read_bytes()


def test_failed_checks_write_nothing(content, precondition, dummy_path, cf, tmp_path):
    r = next(r for r in cf.results if r.skipped)
    bad = changed(cf, r.person_id, r.reading, skipped=[])
    with pytest.raises(WriteRefused, match="problem"):
        write_content(bad, check_content(content, precondition, dummy_path, bad), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_checks_for_fewer_results_are_refused(cf, passed, tmp_path):
    from dataclasses import replace
    with pytest.raises(WriteRefused, match="covered 23"):
        write_content(cf, replace(passed, compared=23), tmp_path)


def test_crash_leaves_nothing(cf, passed, tmp_path, monkeypatch):
    monkeypatch.setattr(dbvo_writer, "csv_text", lambda f: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_content(cf, passed, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_content_file_form_needs_every_person_and_reading(cf):
    with pytest.raises(Exception, match="one content result per person and reading"):
        ContentFile.model_validate(cf.model_dump() | {"results": [r.model_dump() for r in cf.results[:-1]]})
