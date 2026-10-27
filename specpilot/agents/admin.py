"""Administrative agent: runs the whole pipeline for one spec PDF, with the hand-over checks between the steps.

Run:
    python -m specpilot.agents.admin run [spec.pdf]             spec PDF -> expected results
    python -m specpilot.agents.admin verify report.pdf [...]    check vendor reports and store them as submissions
    python -m specpilot.agents.admin status                     the latest run, step by step

A run, step by step (each step calls the agents and tools that already exist, then the checks in checks.py):
    ingest     pdf_ingest                                      -> after_ingest
    extract    extraction agent (Kolibri-1)                     -> after_extract, stale_cards
    pick       rule picker                                      -> after_pick
    translate  translator agent (GLM)                           -> after_translate
    approve    automatic approval, card store                   -> auto_approve, before_branches
    package    branch maker -> value finder -> self-check -> dummy writer
    expected   expected results (level 1) and DBVO answers (level 2)
An error stops the run: the later steps stay 'pending'. Warnings are kept with their step. No step waits for a
person: the criteria card is approved automatically when every check passes.

Verifying a vendor report runs the verification agent between before_verify and after_verify and stores the
result, missing information included, in the submissions table (specpilot/submissions.py).

Every step leaves a row in run_steps and in the audit log (actor "admin").
"""
import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from specpilot import card_store, checks, config, content_review, db, dbvo, llm, submissions
from specpilot.agents import extractor, translator, verification
from specpilot.checks import AUTO_REVIEWER, Problem, error, warning
from specpilot.criteria import CriteriaCard
from specpilot.dummies import build
from specpilot.models import AuditEntry
from specpilot.review import ReviewError
from specpilot.tools import pdf_ingest, rule_picker
from specpilot.tools.branch_maker import make_branches
from specpilot.tools.dbvo_calc import ContentError, check_content, content_file
from specpilot.tools.dbvo_writer import WriteRefused, write_content
from specpilot.tools.dummy_writer import write_outputs
from specpilot.tools.expected_results import check_consistency, compute
from specpilot.tools.expected_writer import write_expected
from specpilot.tools.self_check import check as self_check
from specpilot.tools.value_finder import make_people

log = logging.getLogger("specpilot.admin")

OUT_DIR = config.ROOT / "output"
SEED = 4711
STEPS: list[tuple[str, str]] = [
    ("ingest", "Spec ingested"),
    ("extract", "Rules extracted"),
    ("pick", "Rules picked"),
    ("translate", "Criteria card"),
    ("approve", "Card approved"),
    ("package", "Test package"),
    ("expected", "Expected results"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    pdf         TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    data        TEXT NOT NULL DEFAULT '{}'   -- files later steps and the vendors need: dummy file, expected results
);
CREATE TABLE IF NOT EXISTS run_steps (
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    n           INTEGER NOT NULL,
    name        TEXT NOT NULL,
    label       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    problems    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, n)
);
"""


def _conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Settings:
    pdf: Path
    document: str = config.SPEC_DOCUMENT
    sections: tuple[str, ...] = extractor.DEFAULT_SECTIONS
    card_id: str = "REQ-41-PRECONDITION"
    seed: int = SEED
    out_dir: Path = OUT_DIR
    # Level 2 (DBVO field answers) needs the content card approved. While its sources are still placeholders,
    # an automatic approval would rest on unconfirmed quotes, so it is only done when this is switched on.
    accept_content_placeholders: bool = False


def versioned(conn: sqlite3.Connection, card: CriteriaCard) -> CriteriaCard:
    """The translator always makes v1. Same content as the newest stored version: keep its number, else the next."""
    stored = [e.version for e in card_store.entries(conn) if e.card_id == card.id]
    if not stored:
        return card
    latest = max(stored)
    same = CriteriaCard.model_validate(card.model_dump() | {"version": latest})
    if same.content_hash() == card_store.load(conn, card.id, latest).content_hash():
        return same
    return CriteriaCard.model_validate(card.model_dump() | {"version": latest + 1})


@dataclass
class Run:
    conn: sqlite3.Connection
    settings: Settings
    id: str = field(default_factory=lambda: f"run-{datetime.now():%Y%m%d-%H%M%S-%f}")
    data: dict = field(default_factory=dict)  # stored with the run
    out: dict = field(default_factory=dict)  # handed from one step to the next, not stored

    # --- the steps: each returns (summary, problems) -----------------------------------------------------------

    def ingest(self) -> tuple[str, list[Problem]]:
        s = self.settings
        pages, sections = pdf_ingest.ingest(self.conn, s.pdf, s.document)
        problems = checks.after_ingest(self.conn, s.document, s.sections, checks.pdf_texts(s.pdf))
        return f"{len(pages)} pages, {len(sections)} numbered headings", problems

    def extract(self) -> tuple[str, list[Problem]]:
        s = self.settings
        rules = extractor.run(self.conn, s.sections, s.document)
        problems = checks.after_extract(self.conn, s.sections, rule_picker.CARDS[s.card_id].reason_code, s.document)
        verified = sum(r.quote_verified for r in rules)
        return f"{len(rules)} rules, {verified} quotes verified", problems + checks.stale_cards(self.conn)

    def pick(self) -> tuple[str, list[Problem]]:
        p = self.out["pick"] = rule_picker.pick(self.conn, self.settings.card_id)
        about = len(p.picked) + len(p.excluded)
        return (f"{about + p.other_messages} rules: {p.other_messages} for other messages, {about} about reason "
                f"{p.spec.reason_code} -> {len(p.picked)} picked, {len(p.excluded)} left out with a reason",
                checks.after_pick(self.conn, p))

    def translate(self) -> tuple[str, list[Problem]]:
        card = self.out["card"] = translator.translate(self.conn, self.settings.card_id)
        return (f"{card.id}: {len(card.criteria)} condition(s) from {len(card.source_rule_ids)} rule(s), "
                f"{len(card.unused_rules)} unused", checks.after_translate(card, self.out["pick"]))

    def approve(self) -> tuple[str, list[Problem]]:
        card = versioned(self.conn, self.out["card"])
        approved, problems = checks.auto_approve(card, db.list_requirements(self.conn))
        if approved is None:
            return f"{card.id} v{card.version} not approved", problems
        card_store.save(self.conn, approved)
        self.out["card"] = approved
        self.data["card"] = f"{approved.id} v{approved.version}"
        return f"{approved.id} v{approved.version} approved automatically", checks.before_branches(self.conn, approved)

    def package(self) -> tuple[str, list[Problem]]:
        card = self.out["card"]
        branches = make_branches(card, edges=True)
        d = self.out["dummies"] = build(make_people(branches, self.settings.seed), branches, "admin agent")
        files = write_outputs(d, self_check(card, branches, d), self.settings.out_dir / "dummies")  # refuses on a failed self-check
        self.data.update(dummy_stem=files.sqlite.stem, dummy_file=str(files.sqlite), stripped_csv=str(files.stripped_csv),
                         people=len(d.people))
        outcome = Counter(c.outcome for c in d.coverage)
        return (f"{len(d.coverage)} branches -> {len(d.people)} test people, {outcome['impossible']} impossible "
                f"combination(s); send {files.stripped_csv.name} to the vendors", [])

    def expected(self) -> tuple[str, list[Problem]]:
        card, d = self.out["card"], self.out["dummies"]
        dummy = Path(self.data["dummy_file"])
        out = self.settings.out_dir / "expected"
        e = compute(card, dummy)
        write_expected(e, check_consistency(e, d), out)
        self.data["expected_dir"] = str(out)

        content = dbvo.content_card(dbvo.load())
        unconfirmed = content.placeholders()
        if unconfirmed and not self.settings.accept_content_placeholders:
            return (f"{len(e.results)} expected result(s): must the message be sent", [warning(
                f"no DBVO field answers: the content card has {unconfirmed} unconfirmed source(s); vendors are only "
                f"checked on whether they send the message")])
        try:
            content = content_review.approve(content, AUTO_REVIEWER, accept_placeholders=bool(unconfirmed))
            cf = content_file(content, card, dummy)
            write_content(cf, check_content(content, card, dummy, cf), out)
        except (ContentError, WriteRefused, ReviewError) as err:
            return (f"{len(e.results)} expected result(s): must the message be sent",
                    [warning(f"no DBVO field answers: {err}")])
        problems = [warning(f"DBVO field answers rest on {unconfirmed} unconfirmed source(s)")] if unconfirmed else []
        return f"{len(e.results)} expected result(s), with DBVO field answers", problems

    # --- running them ------------------------------------------------------------------------------------------

    def execute(self, steps: list[tuple[str, str]] = STEPS) -> str:
        """Run the steps in order. Returns the run's status: 'done' or 'failed'."""
        conn = _conn(self.conn)
        with conn:
            conn.execute("INSERT INTO runs (id, started_at, pdf, status) VALUES (?, ?, ?, 'running')",
                         (self.id, _now(), str(self.settings.pdf)))
            conn.executemany("INSERT INTO run_steps (run_id, n, name, label, status) VALUES (?, ?, ?, ?, 'pending')",
                             [(self.id, n, name, label) for n, (name, label) in enumerate(steps, start=1)])
        status = "done"
        for n, (name, label) in enumerate(steps, start=1):
            with conn:
                conn.execute("UPDATE run_steps SET status = 'running', started_at = ? WHERE run_id = ? AND n = ?",
                             (_now(), self.id, n))
            first_call = len(llm.calls)
            try:
                summary, problems = getattr(self, name)()
            except Exception as err:  # noqa: BLE001 - any failure of an agent or tool stops the run, with its message
                log.exception("step %s failed", name)
                summary, problems = "", [error(f"{type(err).__name__}: {err}")]
            failed = bool(checks.errors(problems))
            with conn:
                conn.execute("UPDATE run_steps SET status = ?, finished_at = ?, summary = ?, problems = ? "
                             "WHERE run_id = ? AND n = ?",
                             ("failed" if failed else "done", _now(), summary,
                              json.dumps([asdict(p) for p in problems], ensure_ascii=False), self.id, n))
            calls = llm.calls[first_call:]
            db.log(conn, AuditEntry(
                actor="admin", action=f"{label}: {'FAILED' if failed else summary}", subject_id=self.id,
                model=calls[-1]["model"] if calls else "", cache_hit=bool(calls) and all(c["cache_hit"] for c in calls),
                duration_s=round(sum(c["duration_s"] for c in calls), 2), detail="\n".join(p.text() for p in problems)))
            if failed:
                status = "failed"
                break
        with conn:
            conn.execute("UPDATE runs SET status = ?, finished_at = ?, data = ? WHERE id = ?",
                         (status, _now(), json.dumps(self.data, ensure_ascii=False), self.id))
        return status


def run_pipeline(conn: sqlite3.Connection, pdf: Path, **settings) -> Run:
    r = Run(conn, Settings(pdf=Path(pdf), **settings))
    r.execute()
    return r


def latest_run(conn: sqlite3.Connection) -> dict | None:
    """The newest run with its steps, as plain data (for the status command and the dashboard)."""
    conn = _conn(conn)
    row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    run = dict(row) | {"data": json.loads(row["data"])}
    run["steps"] = [dict(s) | {"problems": json.loads(s["problems"])}
                    for s in conn.execute("SELECT * FROM run_steps WHERE run_id = ? ORDER BY n", (row["id"],))]
    return run


def finish_interrupted(conn: sqlite3.Connection) -> None:
    """A run still 'running' when the program starts was cut off (the program stopped): mark it failed."""
    conn = _conn(conn)
    with conn:
        conn.execute("UPDATE run_steps SET status = 'failed', summary = 'interrupted' WHERE status = 'running'")
        conn.execute("UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'", (_now(),))


# --- vendor reports --------------------------------------------------------------------------------------------------

def verify_reports(conn: sqlite3.Connection, reports: list[Path], expected_dir: Path | None = None,
                   dummy_stem: str | None = None) -> list[int]:
    """Verify each vendor report against the latest run's expected results and store it. Returns submission ids.

    dummy_stem / expected_dir default to the latest run that made a test package. Without one, a report is checked
    against the dummy file it names, and a warning says so.
    """
    run = latest_run(conn)
    data = run["data"] if run else {}
    stem = dummy_stem or data.get("dummy_stem")
    expected_dir = Path(expected_dir or data.get("expected_dir") or verification.EXPECTED_DIR)
    ids = []
    for path in map(Path, reports):
        v = verification.verify(path, expected_dir)
        job_stem = stem or v.dummy_file.removesuffix(".sqlite")
        problems = [] if stem else [warning("no run has made a test package yet: checked against the dummy file "
                                            "the report names")]
        problems += checks.before_verify([path], expected_dir, job_stem)
        problems += checks.after_verify(v, path, expected_dir, job_stem)
        ids.append(submissions.save(conn, v, problems, run["id"] if run else ""))
        db.log(conn, AuditEntry(
            actor="admin", subject_id=v.report, detail="\n".join(p.text() for p in problems),
            action=f"verified {v.report} ({v.vendor or '?'}): {v.verdict}, {v.matches} of {v.cases_compared} match, "
                   f"{len(v.missing_cases)} case(s) with missing information"))
    return ids


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description="Run the pipeline, verify vendor reports, show the latest run.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("pdf", nargs="?", default=str(config.SPEC_PDF))
    r.add_argument("--accept-content-placeholders", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("reports", nargs="+")
    sub.add_parser("status")
    a = p.parse_args(argv)

    conn = db.connect()
    if a.command == "run":
        run_pipeline(conn, Path(a.pdf), accept_content_placeholders=a.accept_content_placeholders)
    elif a.command == "verify":
        verify_reports(conn, [Path(x) for x in a.reports])
        for s in submissions.list_all(conn)[:len(a.reports)]:
            print(f"{s['report']}: {s['verdict']}, {s['matches']}/{s['cases_compared']} match, "
                  f"{s['cases_missing']} case(s) with missing information")
        return 0
    run = latest_run(conn)
    if run is None:
        print("no run yet")
        return 0
    print(f"{run['id']}  {run['status'].upper()}  {run['pdf']}")
    for s in run["steps"]:
        print(f"  {s['status']:<8} {s['label']:<18} {s['summary']}")
        for x in s["problems"]:
            print(f"           [{x['severity'].upper()}] {x['message']}")
    return 0 if run["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
