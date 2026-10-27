"""Vendor submissions: every verified vendor report is kept in the team database, missing information included.

Table submissions, one row per verified report:
    vendor, software, version, the dummy file the vendor ran, the verdict, and the case counts:
    expected / compared / matching / with differences / with missing information (not returned by the vendor),
    has_missing_information = 1 when the vendor did not send enough to judge (missing cases, unreadable report, ...)
    data: the whole verification result plus the hand-over checks run on it
Table submission_flags, one row per flag:
    E1, E2, ... kind 'difference'  a result that does not match the expected result (expected, vendor, why)
    M1, M2, ... kind 'missing'     information the vendor must still send

error_rate   = cases with differences / cases compared
missing_rate = cases with missing information / cases expected
"""
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime

from specpilot.agents.verification import Verification
from specpilot.checks import Problem

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id                      INTEGER PRIMARY KEY,
    checked_at              TEXT NOT NULL,
    run_id                  TEXT NOT NULL,
    report                  TEXT NOT NULL,
    vendor                  TEXT NOT NULL,
    software                TEXT NOT NULL,
    version                 TEXT NOT NULL,
    dummy_file              TEXT NOT NULL,
    verdict                 TEXT NOT NULL,
    cases_expected          INTEGER NOT NULL,
    cases_compared          INTEGER NOT NULL,
    matches                 INTEGER NOT NULL,
    cases_with_differences  INTEGER NOT NULL,
    cases_missing           INTEGER NOT NULL,
    has_missing_information INTEGER NOT NULL,
    data                    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_flags (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    flag          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('difference', 'missing')),
    test_case     TEXT NOT NULL,
    what          TEXT NOT NULL,
    expected      TEXT NOT NULL,
    vendor        TEXT NOT NULL,
    reason        TEXT NOT NULL
);
"""

_CASE = re.compile(r"^(DUMMY-\d+):")


def _conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, v: Verification, problems: list[Problem], run_id: str = "",
         now: datetime | None = None) -> int:
    """Store one verified report with all its flags. Returns the submission id."""
    conn = _conn(conn)
    data = asdict(v) | {"checks": [asdict(p) for p in problems]}
    with conn:
        sid = conn.execute(
            """INSERT INTO submissions (checked_at, run_id, report, vendor, software, version, dummy_file, verdict,
                   cases_expected, cases_compared, matches, cases_with_differences, cases_missing,
                   has_missing_information, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((now or datetime.now()).isoformat(timespec="seconds"), run_id, v.report, v.vendor, v.software, v.version,
             v.dummy_file, v.verdict, v.cases_expected, v.cases_compared, v.matches, len({d.case for d in v.differences}),
             len(v.missing_cases), int(bool(v.missing_information)), json.dumps(data, ensure_ascii=False)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO submission_flags VALUES (?, ?, 'difference', ?, ?, ?, ?, ?)",
            [(sid, d.flag, d.case, d.what, d.expected, d.vendor, d.reason) for d in v.differences])
        conn.executemany(
            "INSERT INTO submission_flags VALUES (?, ?, 'missing', ?, ?, '', '', '')",
            [(sid, m.flag, (_CASE.match(m.what) or [None, ""])[1], m.what) for m in v.missing_information])
    return sid


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def list_all(conn: sqlite3.Connection) -> list[dict]:
    """Every stored submission, newest first, with its rates and the hand-over check problems."""
    rows = _conn(conn).execute("SELECT * FROM submissions ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        s = {k: r[k] for k in r.keys() if k != "data"}
        s["has_missing_information"] = bool(s["has_missing_information"])
        s["error_rate"] = _rate(s["cases_with_differences"], s["cases_compared"])
        s["missing_rate"] = _rate(s["cases_missing"], s["cases_expected"])
        data = json.loads(r["data"])
        s["checks"] = data.get("checks", [])
        s["notes"] = data.get("notes", [])
        out.append(s)
    return out


def flags(conn: sqlite3.Connection, submission_id: int) -> list[dict]:
    rows = _conn(conn).execute("SELECT * FROM submission_flags WHERE submission_id = ? ORDER BY kind, rowid",
                               (submission_id,)).fetchall()
    return [dict(r) for r in rows]


def totals(conn: sqlite3.Connection) -> dict:
    """All submissions together: the numbers the validation page shows at the top."""
    subs = list_all(conn)
    t = {k: sum(s[k] for s in subs) for k in ("cases_expected", "cases_compared", "matches",
                                                "cases_with_differences", "cases_missing")}
    t["submissions"] = len(subs)
    t["with_missing_information"] = sum(s["has_missing_information"] for s in subs)
    t["error_rate"] = _rate(t["cases_with_differences"], t["cases_compared"])
    t["missing_rate"] = _rate(t["cases_missing"], t["cases_expected"])
    return t
