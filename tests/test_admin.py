"""Administrative agent: runs the steps in order, stops at the first error, records everything; stores vendor reports."""
from pathlib import Path

import pytest

from specpilot import card_store, db, submissions
from specpilot.agents import admin, translator
from specpilot.agents.translator import TranslatorError
from specpilot.criteria import CriteriaCard
from specpilot.review import approve
from scripts.make_verification_scenarios import text_pdf
from tests.test_review import RULES, draft
from tests.test_rule_picker import GOLD_RULES, NOISE

FROM_PICK = admin.STEPS[2:]  # the steps after the model-heavy ingest and extraction
STEM = "REQ-41-PRECONDITION-v1-seed4711"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "team.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(c, r)
    yield c
    c.close()


def run_from_pick(conn, tmp_path) -> admin.Run:
    r = admin.Run(conn, admin.Settings(pdf=Path("spec.pdf"), out_dir=tmp_path / "out"))
    r.execute(FROM_PICK)
    return r


def statuses(conn) -> list[tuple[str, str]]:
    return [(s["name"], s["status"]) for s in admin.latest_run(conn)["steps"]]


def test_a_clean_run_goes_through_every_step(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "translate", lambda conn, card_id: draft())
    r = run_from_pick(conn, tmp_path)
    run = admin.latest_run(conn)
    assert run["status"] == "done" and all(status == "done" for _, status in statuses(conn))
    assert run["data"]["dummy_stem"] == STEM and run["data"]["people"] == 34
    assert (tmp_path / "out" / "expected" / f"{STEM}.expected.sqlite").exists()
    assert Path(run["data"]["stripped_csv"]).exists()
    # the content card still has placeholder sources: no DBVO answers, said as a warning
    expected = run["steps"][-1]
    assert [p["severity"] for p in expected["problems"]] == ["warning"] and "no DBVO field answers" in expected["problems"][0]["message"]
    # the card was approved by the agent, not a person, and stored
    assert card_store.load(conn, "REQ-41-PRECONDITION", 1).reviewer == admin.AUTO_REVIEWER
    assert [a.actor for a in db.list_audit(conn, r.id)] == ["admin"] * len(FROM_PICK)


def test_a_failing_agent_stops_the_run(conn, tmp_path, monkeypatch):
    def broken(conn, card_id):
        raise TranslatorError("no valid card after 3 attempts")
    monkeypatch.setattr(translator, "translate", broken)
    run_from_pick(conn, tmp_path)
    assert statuses(conn) == [("pick", "done"), ("translate", "failed"), ("approve", "pending"),
                              ("package", "pending"), ("expected", "pending")]
    run = admin.latest_run(conn)
    assert run["status"] == "failed"
    assert run["steps"][1]["problems"] == [{"severity": "error", "message": "TranslatorError: no valid card after 3 attempts"}]


def test_a_card_the_checks_do_not_trust_is_not_approved(conn, tmp_path, monkeypatch):
    doubtful = CriteriaCard.model_validate(draft().model_dump() | {"flags": ["Cross-check run failed, card is unconfirmed: timeout"]})
    monkeypatch.setattr(translator, "translate", lambda conn, card_id: doubtful)
    run_from_pick(conn, tmp_path)
    assert statuses(conn)[2:] == [("approve", "failed"), ("package", "pending"), ("expected", "pending")]
    assert card_store.entries(conn) == []


def test_card_versions(conn):
    assert admin.versioned(conn, draft()).version == 1
    card_store.save(conn, approve(draft(), "x", RULES))
    assert admin.versioned(conn, draft()).version == 1  # same content: same version
    other = CriteriaCard.model_validate(draft().model_dump() | {"logic": "OR"})
    assert admin.versioned(conn, other).version == 2


def test_an_interrupted_run_is_marked_failed(conn, tmp_path):
    r = admin.Run(conn, admin.Settings(pdf=Path("spec.pdf")))
    admin._conn(conn).execute("INSERT INTO runs (id, started_at, pdf, status) VALUES (?, '2026-10-27', 'x', 'running')", (r.id,))
    admin.finish_interrupted(conn)
    assert admin.latest_run(conn)["status"] == "failed"


# --- the ingest step on a real (small) PDF -----------------------------------------------------------------------------

def spec_pdf(path: Path, second_page: str) -> Path:
    head = lambda n: ["Datenbaustein DBVO", f"Stand: 10.03.2026     Seite {n} von 2       Version 13.4"]  # noqa: E731
    return text_pdf(head(1) + ["1. Einleitung", "1.1 Datenbaustein DBVO",
                               "Die Anfrage darf nur gesendet werden, wenn eine Bescheinigung vorliegt."]
                    + head(2) + [second_page, ""], path, per_page=5)


def test_ingest_step(tmp_path):
    conn = db.connect(tmp_path / "spec.db")
    good = spec_pdf(tmp_path / "good.pdf", "Die Zeiten muessen zusammen mindestens 30 Tage umfassen.")
    admin.Run(conn, admin.Settings(pdf=good, document="Test", sections=("1.1",))).execute(admin.STEPS[:1])
    step = admin.latest_run(conn)["steps"][0]
    assert step["status"] == "done" and step["summary"] == "2 pages, 2 numbered headings" and step["problems"] == []

    blank = spec_pdf(tmp_path / "blank.pdf", "")
    admin.Run(conn, admin.Settings(pdf=blank, document="Test2", sections=("1.1",))).execute(admin.STEPS[:1])
    step = admin.latest_run(conn)["steps"][0]
    assert step["status"] == "failed"
    assert step["problems"][0]["message"] == "p.2 in § 1.1 has no text: rules on it cannot be extracted"


# --- vendor reports ------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    from scripts.make_verification_scenarios import make
    root = tmp_path_factory.mktemp("scenarios")
    return make(root / "out", root / "vendor")


def test_vendor_reports_are_stored_with_their_missing_information(conn, scenarios):
    reports = [scenarios["certified"], scenarios["rejected"], scenarios["missing_information"]]
    ids = admin.verify_reports(conn, reports, expected_dir=scenarios["expected"], dummy_stem=STEM)
    stored = {s["id"]: s for s in submissions.list_all(conn)}
    certified, rejected, incomplete = (stored[i] for i in ids)

    assert (certified["verdict"], certified["has_missing_information"], certified["checks"]) == ("CERTIFIED", False, [])
    assert (rejected["cases_with_differences"], rejected["error_rate"]) == (2, round(2 / 34, 4))
    assert incomplete["has_missing_information"] and incomplete["cases_missing"] == 4
    assert incomplete["missing_rate"] == round(4 / 34, 4) and incomplete["error_rate"] == 0.0
    missing = [f for f in submissions.flags(conn, incomplete["id"]) if f["kind"] == "missing"]
    assert [(f["flag"], f["test_case"]) for f in missing] == [
        ("M1", "DUMMY-0004"), ("M2", "DUMMY-0014"), ("M3", "DUMMY-0024"), ("M4", "DUMMY-0034")]
    assert [f["flag"] for f in submissions.flags(conn, rejected["id"])] == ["E1", "E2"]

    t = submissions.totals(conn)
    assert (t["submissions"], t["cases_compared"], t["matches"], t["cases_with_differences"], t["cases_missing"],
            t["with_missing_information"]) == (3, 98, 96, 2, 4, 1)
    assert t["error_rate"] == round(2 / 98, 4)
    assert len([a for a in db.list_audit(conn) if a.action.startswith("verified ")]) == 3


def test_a_report_without_a_run_is_checked_against_the_file_it_names(conn, scenarios):
    [sid] = admin.verify_reports(conn, [scenarios["certified"]], expected_dir=scenarios["expected"])
    s = submissions.list_all(conn)[0]
    assert s["id"] == sid and s["verdict"] == "CERTIFIED"
    assert [c["message"] for c in s["checks"]] == [
        "no run has made a test package yet: checked against the dummy file the report names"]


def test_an_unreadable_report_is_stored_as_missing_information(conn, tmp_path, scenarios):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 but nothing else")
    admin.verify_reports(conn, [bad], expected_dir=scenarios["expected"], dummy_stem=STEM)
    s = submissions.list_all(conn)[0]
    assert s["verdict"] == "MISSING INFORMATION" and s["has_missing_information"] and s["cases_compared"] == 0
    assert any(f["kind"] == "missing" for f in submissions.flags(conn, s["id"]))
