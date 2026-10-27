"""SQLite storage for the whole chain: spec page -> rule -> test case -> expected result -> finding.

Each model is stored whole as JSON in a `data` column, so the tables never need
changing when a model gains a field. The columns next to it are copies of the
fields the dashboard filters and joins on (section, verdict, ...).
"""
import sqlite3
from pathlib import Path

from specpilot import config
from specpilot.models import (
    AuditEntry,
    ExpectedResult,
    Finding,
    Requirement,
    TestCase,
    TopicCase,
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

-- Numbered headings (3.12, 2.2.2.5, ...) and the pages they span
CREATE TABLE IF NOT EXISTS spec_sections (
    document   TEXT NOT NULL,
    number     TEXT NOT NULL,
    title      TEXT NOT NULL,
    first_page INTEGER NOT NULL,
    last_page  INTEGER NOT NULL,
    PRIMARY KEY (document, number)
);

CREATE TABLE IF NOT EXISTS requirements (
    id      TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    page    INTEGER NOT NULL,
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

-- Test cases the testing agent wrote per topic (spec section)
CREATE TABLE IF NOT EXISTS topic_cases (
    id      TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expected_results (
    test_case_id TEXT PRIMARY KEY REFERENCES test_cases(id) ON DELETE CASCADE,
    data         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    test_case_id   TEXT NOT NULL,  -- a test_cases or topic_cases id
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
    _drop_status(conn)
    _findings_without_case_fk(conn)
    return conn


def _drop_status(conn: sqlite3.Connection) -> None:
    """Databases made before rules lost their approval status: remove the column and the JSON keys."""
    if "status" in [r["name"] for r in conn.execute("PRAGMA table_info(requirements)")]:
        with conn:
            conn.execute("ALTER TABLE requirements DROP COLUMN status")
            conn.execute("UPDATE requirements SET data = json_remove(data, '$.status', '$.reviewer_note')")


def _findings_without_case_fk(conn: sqlite3.Connection) -> None:
    """Databases made before topic test cases: findings may only point to test_cases. Rebuild without that link."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'findings'").fetchone()["sql"]
    if "REFERENCES test_cases" not in sql:
        return
    with conn:
        conn.execute("ALTER TABLE findings RENAME TO findings_old")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO findings SELECT * FROM findings_old")
        conn.execute("DROP TABLE findings_old")


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


def save_sections(conn: sqlite3.Connection, document: str, sections: list[dict]) -> None:
    """Replace all sections of a document. Each dict: number, title, first_page, last_page."""
    with conn:
        conn.execute("DELETE FROM spec_sections WHERE document = ?", (document,))
        conn.executemany(
            "INSERT INTO spec_sections (document, number, title, first_page, last_page) VALUES (?, ?, ?, ?, ?)",
            [(document, s["number"], s["title"], s["first_page"], s["last_page"]) for s in sections],
        )


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    """Every ingested document with its page and section count."""
    rows = conn.execute(
        """SELECT p.document, COUNT(*) AS pages,
                  (SELECT COUNT(*) FROM spec_sections s WHERE s.document = p.document) AS sections
           FROM spec_pages p GROUP BY p.document ORDER BY p.document"""
    )
    return [dict(r) for r in rows]


def list_sections(conn: sqlite3.Connection, document: str) -> list[dict]:
    rows = conn.execute("SELECT number, title, first_page, last_page FROM spec_sections WHERE document = ? ORDER BY first_page", (document,))
    return [dict(r) for r in rows]


def section_pages(conn: sqlite3.Connection, document: str, number: str) -> list[int]:
    """All pages of a section including its subsections, e.g. '3.12' -> [135, ..., 147]."""
    row = conn.execute(
        """SELECT MIN(first_page) AS first, MAX(last_page) AS last FROM spec_sections
           WHERE document = ? AND (number = ? OR number LIKE ? || '.%')""",
        (document, number, number),
    ).fetchone()
    return list(range(row["first"], row["last"] + 1)) if row["first"] is not None else []


# --- rules -------------------------------------------------------------------

def save_requirement(conn: sqlite3.Connection, r: Requirement) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO requirements (id, section, page, data) VALUES (?, ?, ?, ?)",
            (r.id, r.section, r.page, r.model_dump_json()),
        )


def get_requirement(conn: sqlite3.Connection, rule_id: str) -> Requirement | None:
    row = conn.execute("SELECT data FROM requirements WHERE id = ?", (rule_id,)).fetchone()
    return Requirement.model_validate_json(row["data"]) if row else None


def list_requirements(
    conn: sqlite3.Connection, reason_code: str | None = None, actor: str | None = None, verified: bool | None = None
) -> list[Requirement]:
    """reason_code picks the rules for one message (e.g. "41"), plus the rules that apply to every message.

    What the testing agent reads: list_requirements(conn, reason_code="41", actor="employer", verified=True)
    """
    sql, args = "SELECT data FROM requirements WHERE 1=1", []
    if actor is not None:
        sql += " AND json_extract(data, '$.actor') = ?"
        args.append(actor)
    if verified is not None:
        sql += " AND json_extract(data, '$.quote_verified') = ?"
        args.append(int(verified))
    if reason_code is not None:
        sql += """ AND (json_array_length(data, '$.reason_codes') = 0
                   OR EXISTS (SELECT 1 FROM json_each(data, '$.reason_codes') WHERE value = ?))"""
        args.append(reason_code)
    return [Requirement.model_validate_json(r["data"]) for r in conn.execute(sql + " ORDER BY id", args)]


def delete_section(conn: sqlite3.Connection, section: str) -> int:
    """Before a new extraction run: drop the section's old rules (and the test cases built on them)."""
    cases = """SELECT c.test_case_id FROM case_requirements c JOIN requirements r ON r.id = c.requirement_id
               WHERE r.section = ?"""
    with conn:
        conn.execute(f"DELETE FROM findings WHERE test_case_id IN ({cases})", (section,))
        conn.execute(f"DELETE FROM test_cases WHERE id IN ({cases})", (section,))
        conn.execute("DELETE FROM topic_cases WHERE section = ?", (section,))
        return conn.execute("DELETE FROM requirements WHERE section = ?", (section,)).rowcount


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
    """Per testable rule (employer, verified quote): how many positive / negative / boundary cases. Zero = a gap."""
    rows = conn.execute(
        """SELECT r.id AS requirement_id,
                  SUM(t.kind = 'positive') AS positive,
                  SUM(t.kind = 'negative') AS negative,
                  SUM(t.kind = 'boundary') AS boundary
           FROM requirements r
           LEFT JOIN case_requirements c ON c.requirement_id = r.id
           LEFT JOIN test_cases t ON t.id = c.test_case_id
           WHERE json_extract(r.data, '$.actor') = 'employer' AND json_extract(r.data, '$.quote_verified') = 1
           GROUP BY r.id ORDER BY r.id"""
    ).fetchall()
    return [{k: (row[k] or 0) if k != "requirement_id" else row[k] for k in row.keys()} for row in rows]


def save_topic_cases(conn: sqlite3.Connection, section: str, cases: list[TopicCase]) -> None:
    """Replace a topic's test cases with a new set."""
    with conn:
        conn.execute("DELETE FROM topic_cases WHERE section = ?", (section,))
        conn.executemany(
            "INSERT INTO topic_cases (id, section, data) VALUES (?, ?, ?)",
            [(c.id, section, c.model_dump_json()) for c in cases],
        )


def list_topic_cases(conn: sqlite3.Connection, section: str | None = None) -> list[TopicCase]:
    if section is None:
        rows = conn.execute("SELECT data FROM topic_cases ORDER BY section, id").fetchall()
    else:
        rows = conn.execute("SELECT data FROM topic_cases WHERE section = ? ORDER BY id", (section,)).fetchall()
    return [TopicCase.model_validate_json(r["data"]) for r in rows]


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


def reset_run(conn: sqlite3.Connection) -> None:
    """Start fresh: drop rules, test cases, expected results and findings. The loaded spec and the audit log stay."""
    with conn:
        for table in ("findings", "expected_results", "case_requirements", "test_cases", "topic_cases", "requirements"):
            conn.execute(f"DELETE FROM {table}")


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
