"""SQLite storage for the whole chain: spec page -> rule -> test case -> expected result -> finding.

Each model is stored whole as JSON in a `data` column, so the tables never need
changing when a model gains a field. The columns next to it are copies of the
fields the dashboard filters and joins on (status, verdict, ...).
"""
import sqlite3
from pathlib import Path

from specpilot import config
from specpilot.models import (
    AuditEntry,
    ExpectedResult,
    Finding,
    Requirement,
    RuleStatus,
    TestCase,
    Verdict,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS spec_pages (
    document TEXT NOT NULL,
    page     INTEGER NOT NULL,
    section  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL,
    PRIMARY KEY (document, page)
);
-- Full-text index over spec_pages.text; kept in sync by save_page
CREATE VIRTUAL TABLE IF NOT EXISTS spec_fts USING fts5(
    text, content='spec_pages', tokenize='unicode61 remove_diacritics 0'
);

CREATE TABLE IF NOT EXISTS requirements (
    id      TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    page    INTEGER NOT NULL,
    status  TEXT NOT NULL,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_cases (
    id   TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
-- Which test case covers which rule (the coverage matrix)
CREATE TABLE IF NOT EXISTS case_requirements (
    test_case_id   TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
    requirement_id TEXT NOT NULL REFERENCES requirements(id),
    PRIMARY KEY (test_case_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS expected_results (
    test_case_id TEXT PRIMARY KEY REFERENCES test_cases(id) ON DELETE CASCADE,
    data         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    test_case_id   TEXT NOT NULL REFERENCES test_cases(id),
    verdict        TEXT NOT NULL,
    layer          TEXT NOT NULL,
    requirement_id TEXT,
    data           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    model      TEXT NOT NULL,
    cache_hit  INTEGER NOT NULL,
    duration_s REAL NOT NULL,
    data       TEXT NOT NULL
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and create if needed) the database. Tests pass a temporary path or ':memory:'."""
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- spec text ---------------------------------------------------------------

def save_page(conn: sqlite3.Connection, document: str, page: int, text: str, section: str = "") -> None:
    with conn:
        old = conn.execute("SELECT rowid, text FROM spec_pages WHERE document = ? AND page = ?", (document, page)).fetchone()
        if old:
            # External-content FTS: the old text must be removed from the index explicitly
            conn.execute("INSERT INTO spec_fts(spec_fts, rowid, text) VALUES ('delete', ?, ?)", (old["rowid"], old["text"]))
            conn.execute("UPDATE spec_pages SET text = ?, section = ? WHERE rowid = ?", (text, section, old["rowid"]))
            rowid = old["rowid"]
        else:
            rowid = conn.execute(
                "INSERT INTO spec_pages (document, page, section, text) VALUES (?, ?, ?, ?)", (document, page, section, text)
            ).lastrowid
        conn.execute("INSERT INTO spec_fts(rowid, text) VALUES (?, ?)", (rowid, text))


def get_page(conn: sqlite3.Connection, document: str, page: int) -> str | None:
    row = conn.execute("SELECT text FROM spec_pages WHERE document = ? AND page = ?", (document, page)).fetchone()
    return row["text"] if row else None


def search_pages(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Full-text search. Returns the best pages first with a short snippet around the hit."""
    rows = conn.execute(
        """SELECT p.document, p.page, p.section, snippet(spec_fts, 0, '[', ']', ' … ', 12) AS snippet
           FROM spec_fts JOIN spec_pages p ON p.rowid = spec_fts.rowid
           WHERE spec_fts MATCH ? ORDER BY rank LIMIT ?""",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- rules -------------------------------------------------------------------

def save_requirement(conn: sqlite3.Connection, r: Requirement) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO requirements (id, section, page, status, data) VALUES (?, ?, ?, ?, ?)",
            (r.id, r.section, r.page, r.status.value, r.model_dump_json()),
        )


def get_requirement(conn: sqlite3.Connection, rule_id: str) -> Requirement | None:
    row = conn.execute("SELECT data FROM requirements WHERE id = ?", (rule_id,)).fetchone()
    return Requirement.model_validate_json(row["data"]) if row else None


def list_requirements(conn: sqlite3.Connection, status: RuleStatus | None = None) -> list[Requirement]:
    if status is None:
        rows = conn.execute("SELECT data FROM requirements ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT data FROM requirements WHERE status = ? ORDER BY id", (status.value,)).fetchall()
    return [Requirement.model_validate_json(r["data"]) for r in rows]


def set_status(conn: sqlite3.Connection, rule_id: str, status: RuleStatus, note: str = "") -> Requirement:
    """The examiner's decision at gate 1."""
    r = get_requirement(conn, rule_id)
    if r is None:
        raise KeyError(rule_id)
    r = r.model_copy(update={"status": status, "reviewer_note": note or r.reviewer_note})
    save_requirement(conn, r)
    return r


# --- test cases and expected results -------------------------------------------

def save_test_case(conn: sqlite3.Connection, tc: TestCase) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO test_cases (id, kind, data) VALUES (?, ?, ?)", (tc.id, tc.kind.value, tc.model_dump_json()))
        conn.execute("DELETE FROM case_requirements WHERE test_case_id = ?", (tc.id,))
        conn.executemany(
            "INSERT INTO case_requirements (test_case_id, requirement_id) VALUES (?, ?)",
            [(tc.id, rid) for rid in tc.requirement_ids],
        )


def get_test_case(conn: sqlite3.Connection, case_id: str) -> TestCase | None:
    row = conn.execute("SELECT data FROM test_cases WHERE id = ?", (case_id,)).fetchone()
    return TestCase.model_validate_json(row["data"]) if row else None


def list_test_cases(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[TestCase]:
    if requirement_id is None:
        rows = conn.execute("SELECT data FROM test_cases ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            """SELECT t.data FROM test_cases t JOIN case_requirements c ON c.test_case_id = t.id
               WHERE c.requirement_id = ? ORDER BY t.id""",
            (requirement_id,),
        ).fetchall()
    return [TestCase.model_validate_json(r["data"]) for r in rows]


def coverage(conn: sqlite3.Connection) -> list[dict]:
    """Per approved rule: how many positive / negative / boundary cases exist. Zero anywhere = a gap."""
    rows = conn.execute(
        """SELECT r.id AS requirement_id,
                  SUM(t.kind = 'positive') AS positive,
                  SUM(t.kind = 'negative') AS negative,
                  SUM(t.kind = 'boundary') AS boundary
           FROM requirements r
           LEFT JOIN case_requirements c ON c.requirement_id = r.id
           LEFT JOIN test_cases t ON t.id = c.test_case_id
           WHERE r.status = 'approved'
           GROUP BY r.id ORDER BY r.id"""
    ).fetchall()
    return [{k: (row[k] or 0) if k != "requirement_id" else row[k] for k in row.keys()} for row in rows]


def save_expected(conn: sqlite3.Connection, e: ExpectedResult) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO expected_results (test_case_id, data) VALUES (?, ?)", (e.test_case_id, e.model_dump_json()))


def get_expected(conn: sqlite3.Connection, case_id: str) -> ExpectedResult | None:
    row = conn.execute("SELECT data FROM expected_results WHERE test_case_id = ?", (case_id,)).fetchone()
    return ExpectedResult.model_validate_json(row["data"]) if row else None


# --- findings ------------------------------------------------------------------

def add_finding(conn: sqlite3.Connection, run_id: str, f: Finding) -> None:
    """run_id groups the findings of one validation run, so reruns don't mix with old results."""
    with conn:
        conn.execute(
            "INSERT INTO findings (run_id, test_case_id, verdict, layer, requirement_id, data) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, f.test_case_id, f.verdict.value, f.layer.value, f.requirement_id, f.model_dump_json()),
        )


def list_findings(conn: sqlite3.Connection, run_id: str | None = None, verdict: Verdict | None = None) -> list[Finding]:
    sql, args = "SELECT data FROM findings WHERE 1=1", []
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    if verdict is not None:
        sql += " AND verdict = ?"
        args.append(verdict.value)
    return [Finding.model_validate_json(r["data"]) for r in conn.execute(sql + " ORDER BY id", args)]


# --- audit -------------------------------------------------------------------

def log(conn: sqlite3.Connection, e: AuditEntry) -> None:
    with conn:
        conn.execute(
            """INSERT INTO audit (at, actor, action, subject_id, model, cache_hit, duration_s, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.at.isoformat(), e.actor, e.action, e.subject_id, e.model, int(e.cache_hit), e.duration_s, e.model_dump_json()),
        )


def list_audit(conn: sqlite3.Connection, subject_id: str | None = None) -> list[AuditEntry]:
    if subject_id is None:
        rows = conn.execute("SELECT data FROM audit ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT data FROM audit WHERE subject_id = ? ORDER BY id", (subject_id,)).fetchall()
    return [AuditEntry.model_validate_json(r["data"]) for r in rows]
