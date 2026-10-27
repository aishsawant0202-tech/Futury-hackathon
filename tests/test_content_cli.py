"""The --content command: dummy file + precondition card + content card -> checked DBVO answers."""
import json
import shutil
from pathlib import Path

import pytest

from specpilot import content_review, dbvo
from specpilot.review import approve, save_card
from specpilot.tools import expected_results
from specpilot.tools.dbvo_writer import load
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """Approved precondition card, approved content card and a content-edge dummy file, saved as files."""
    from specpilot.dummies import build
    from specpilot.tools import scenarios
    from specpilot.tools.branch_maker import make_branches
    from specpilot.tools.dummy_writer import write_outputs
    from specpilot.tools.self_check import check
    from specpilot.tools.value_finder import make_people
    folder = tmp_path_factory.mktemp("made")
    pre = approve(draft(), "Examiner A", RULES)
    sc = approve(scenarios.build(pre, RULES), "Examiner A", RULES)
    s = make_branches(sc, background=pre)
    d = build(make_people(s, 4711), s, "test")
    dummy = write_outputs(d, check(sc, s, d, pre), folder / "dummies").sqlite
    save_card(pre, folder / "card.json")
    dbvo.save_card(content_review.approve(dbvo.content_card(dbvo.load()), "Examiner A", accept_placeholders=True),
                   folder / "content.json")
    return folder


@pytest.fixture
def files(made, tmp_path):
    shutil.copytree(made, tmp_path / "w")
    w = tmp_path / "w"
    return w / "dummies" / "REQ-41-CONTENT-SCENARIOS-v1-seed4711.sqlite", w / "card.json", w / "content.json"


def run(argv, capsys):
    code = expected_results.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_writes_the_dbvo_answers(files, tmp_path, capsys):
    dummy, card, content = files
    code, out, _ = run([dummy, card, "--content", content, "--out", tmp_path / "expected"], capsys)
    assert code == 0
    assert "12 people x 2 readings = 24 DBVO results" in out
    assert "CONTENT CHECKS PASSED: 24 answer(s) checked against the content rules" in out
    assert "approved accepting 14 PLACEHOLDER source(s): these answers are provisional" in out
    names = sorted(p.name for p in (tmp_path / "expected").iterdir())
    assert names == ["REQ-41-CONTENT-SCENARIOS-v1-seed4711.dbvo.csv", "REQ-41-CONTENT-SCENARIOS-v1-seed4711.dbvo.sqlite"]
    assert len(load(tmp_path / "expected" / names[1]).results) == 24


def test_dry_run_and_explain_show_both_readings(files, tmp_path, capsys):
    dummy, card, content = files
    from specpilot.tools.dummy_writer import load as load_dummy
    neutral = load_dummy(dummy).people[0].neutral_id
    code, out, _ = run([dummy, card, "--content", content, "--dry-run", "--explain", neutral,
                        "--out", tmp_path / "expected"], capsys)
    assert code == 0 and "Dry run passed: nothing was written." in out
    assert f"{neutral} (" in out and "reading p.135" in out and "reading p.136" in out and "ANZAHL-AU = " in out
    assert not (tmp_path / "expected").exists()


def test_without_content_it_is_level_1_as_before(files, capsys):
    """The content-edge file has no level 1 answers of its own: level 1 refuses it (another card)."""
    dummy, card, _ = files
    code, _, err = run([dummy, card, "--dry-run"], capsys)
    assert code == 1 and "fingerprints differ" in err


def test_unapproved_content_card_is_refused(files, capsys):
    dummy, card, content = files
    dbvo.save_card(dbvo.content_card(dbvo.load()), content)
    code, _, err = run([dummy, card, "--content", content, "--dry-run"], capsys)
    assert code == 1 and "is not approved" in err


def test_content_card_changed_after_approval_is_refused(files, capsys):
    dummy, card, content = files
    data = json.loads(content.read_text(encoding="utf-8"))
    next(r for r in data["rules"] if r["key"] == "gap_months")["values"]["p.135"] = 7
    content.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run([dummy, card, "--content", content, "--dry-run"], capsys)
    assert code == 1 and "changed after it was approved" in err


def test_missing_and_wrong_files(files, tmp_path, capsys):
    dummy, card, content = files
    assert run([dummy, card, "--content", tmp_path / "nope.json"], capsys)[0] == 1
    save_card(draft(), tmp_path / "draft.json")
    code, _, err = run([dummy, tmp_path / "draft.json", "--content", content, "--dry-run"], capsys)
    assert code == 1 and "not approved" in err


def test_unknown_person_to_explain(files, capsys):
    dummy, card, content = files
    code, _, err = run([dummy, card, "--content", content, "--dry-run", "--explain", "DUMMY-9999"], capsys)
    assert code == 1 and "no person DUMMY-9999" in err


def test_refuses_to_write_into_the_dummy_folder(files, capsys):
    dummy, card, content = files
    code, _, err = run([dummy, card, "--content", content, "--out", dummy.parent], capsys)
    assert code == 1 and "dummy file's folder" in err


def test_changed_dummy_file_fails_the_checks_even_on_a_dry_run(files, tmp_path, capsys):
    """A content person changed after the self-check so that they no longer send: the checks must fail and
    nothing may be written, also on a dry run."""
    import sqlite3
    dummy, card, content = files
    conn = sqlite3.connect(dummy)
    with conn:
        conn.execute("UPDATE dummy_people SET statutory_insured = 0 WHERE neutral_id = "
                     "(SELECT MIN(neutral_id) FROM dummy_people)")
    conn.close()
    code, out, err = run([dummy, card, "--content", content, "--dry-run"], capsys)
    assert code == 1 and "CONTENT CHECKS FAILED" in out and "Dry run passed" not in out
    code, _, err = run([dummy, card, "--content", content, "--out", tmp_path / "expected"], capsys)
    assert code == 1 and "nothing was written" in err and not (tmp_path / "expected").exists()
