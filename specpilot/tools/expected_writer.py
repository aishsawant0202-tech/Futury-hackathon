"""Writes the expected results (the ANSWERS) to their own files, apart from the dummy file.

    output/expected/<dummy file stem>.expected.sqlite
        expected_meta        card, version, fingerprint, seed, the dummy file and its fingerprint, consistency result
        expected_results     one row per person and reading: must the message be sent (1/0)
        expected_conditions  one row per person, reading and condition: the person's recomputed value, met (1/0),
                             the rule ids and German quote the condition rests on
    output/expected/<dummy file stem>.expected.csv
        one row per person and reading: the answer and, per condition, what it says, the value and met (1/0).
        Plain ASCII (the German quotes are in the SQL file only).

Written only if the consistency check passed, under temporary names, read back and compared, then renamed
together: a half-written or unverified answers file never carries the real name. No timestamps.
"""
import csv
import io
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from specpilot import config
from specpilot.expected import ConditionResult, ExpectedFile, ExpectedResult, reading_key

OUT_DIR = config.ROOT / "output" / "expected"
VERSION = "expected_writer 0.1"


class WriteRefused(Exception):
    """Nothing was written; the message says why."""


@dataclass(frozen=True)
class Outputs:
    sqlite: Path
    csv: Path


def output_paths(e: ExpectedFile, out_dir: Path = OUT_DIR) -> Outputs:
    stem = Path(out_dir) / Path(e.dummy_file).name.removesuffix(".sqlite")
    return Outputs(Path(f"{stem}.expected.sqlite"), Path(f"{stem}.expected.csv"))


SCHEMA = """
CREATE TABLE expected_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE expected_results (
    person_id        TEXT NOT NULL,
    neutral_id       TEXT NOT NULL,
    reading          TEXT NOT NULL,
    reading_json     TEXT NOT NULL,
    position         INTEGER NOT NULL UNIQUE,
    message_expected INTEGER NOT NULL CHECK (message_expected IN (0, 1)),
    PRIMARY KEY (person_id, reading)
);
CREATE TABLE expected_conditions (
    person_id    TEXT NOT NULL,
    reading      TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    fact         TEXT NOT NULL,
    op           TEXT NOT NULL,
    limit_json   TEXT NOT NULL,
    value_json   TEXT NOT NULL,
    met          INTEGER NOT NULL CHECK (met IN (0, 1)),
    rule_ids     TEXT NOT NULL,
    quote        TEXT NOT NULL,
    PRIMARY KEY (person_id, reading, criterion_id),
    FOREIGN KEY (person_id, reading) REFERENCES expected_results (person_id, reading)
);
"""


def _write_sqlite(path: Path, e: ExpectedFile, consistency_line: str, notes: list[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        meta = {"card_id": e.card_id, "card_version": str(e.card_version), "card_hash": e.card_hash,
                "seed": str(e.seed), "dummy_file": e.dummy_file, "dummy_sha256": e.dummy_sha256, "logic": e.logic,
                "n_criteria": str(e.n_criteria), "made_by": e.made_by, "written_by": VERSION,
                "person_ids": json.dumps(e.person_ids), "readings": json.dumps(e.readings, sort_keys=True),
                "consistency": consistency_line, "notes": "\n".join(notes)}
        with conn:
            conn.executemany("INSERT INTO expected_meta VALUES (?, ?)", sorted(meta.items()))
            for i, r in enumerate(e.results, start=1):
                key = reading_key(r.reading)
                conn.execute("INSERT INTO expected_results VALUES (?, ?, ?, ?, ?, ?)",
                             (r.person_id, r.neutral_id, key, json.dumps(r.reading, sort_keys=True), i,
                              int(r.message_expected)))
                conn.executemany("INSERT INTO expected_conditions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 [(r.person_id, key, c.criterion_id, c.fact, c.op, json.dumps(c.limit),
                                   json.dumps(c.value), int(c.met), json.dumps(c.rule_ids), c.quote)
                                  for c in r.conditions])
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise WriteRefused("broken links in the written answers file")
    finally:
        conn.close()


def load(path: Path) -> ExpectedFile:
    """Read an answers file back into the checked form."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = dict(conn.execute("SELECT key, value FROM expected_meta").fetchall())
        conds: dict[tuple[str, str], list[ConditionResult]] = {}
        for c in conn.execute("SELECT * FROM expected_conditions ORDER BY person_id, reading, "
                              "CAST(SUBSTR(criterion_id, 2) AS INTEGER)"):
            conds.setdefault((c["person_id"], c["reading"]), []).append(ConditionResult(
                criterion_id=c["criterion_id"], fact=c["fact"], op=c["op"], limit=json.loads(c["limit_json"]),
                value=json.loads(c["value_json"]), met=bool(c["met"]), rule_ids=json.loads(c["rule_ids"]),
                quote=c["quote"]))
        results = [ExpectedResult(person_id=r["person_id"], neutral_id=r["neutral_id"],
                                  reading=json.loads(r["reading_json"]), logic=meta["logic"],
                                  conditions=conds.get((r["person_id"], r["reading"]), []),
                                  message_expected=bool(r["message_expected"]))
                   for r in conn.execute("SELECT * FROM expected_results ORDER BY position")]
    finally:
        conn.close()
    return ExpectedFile(card_id=meta["card_id"], card_version=int(meta["card_version"]), card_hash=meta["card_hash"],
                        seed=int(meta["seed"]), dummy_file=meta["dummy_file"], dummy_sha256=meta["dummy_sha256"],
                        logic=meta["logic"], n_criteria=int(meta["n_criteria"]), made_by=meta["made_by"],
                        person_ids=json.loads(meta["person_ids"]), readings=json.loads(meta["readings"]),
                        results=results)


def csv_text(e: ExpectedFile) -> str:
    per = [f"C{i}_{part}" for i in range(1, e.n_criteria + 1) for part in ("condition", "value", "met")]
    columns = ["neutral_id", "person_id", "reading", "message_expected"] + per + ["card_id", "card_version", "seed",
                                                                                  "dummy_file"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in e.results:
        row = {"neutral_id": r.neutral_id, "person_id": r.person_id, "reading": reading_key(r.reading),
               "message_expected": int(r.message_expected), "card_id": e.card_id, "card_version": e.card_version,
               "seed": e.seed, "dummy_file": e.dummy_file}
        for c in r.conditions:
            row |= {f"{c.criterion_id}_condition": c.condition(),
                    f"{c.criterion_id}_value": int(c.value) if isinstance(c.value, bool) else c.value,
                    f"{c.criterion_id}_met": int(c.met)}
        w.writerow(row)
    return buf.getvalue()


def write_expected(e: ExpectedFile, consistency, out_dir: Path = OUT_DIR) -> Outputs:
    """Write the answers, only if the consistency check passed and covered every person."""
    if not consistency.ok:
        raise WriteRefused(f"the consistency check found {len(consistency.problems)} problem(s); nothing was written:\n"
                           + "\n".join(f"  {p.text()}" for p in consistency.problems[:20]))
    if consistency.compared != len(e.person_ids):
        raise WriteRefused(f"the consistency check compared {consistency.compared} answer(s), the file has "
                           f"{len(e.person_ids)} people")
    final = output_paths(e, out_dir)
    final.sqlite.parent.mkdir(parents=True, exist_ok=True)
    pairs = [(f, f.parent / f".{f.name}.tmp") for f in (final.sqlite, final.csv)]
    for _, tmp in pairs:
        tmp.unlink(missing_ok=True)
    try:
        _write_sqlite(pairs[0][1], e, consistency.text().splitlines()[0], consistency.notes)
        if load(pairs[0][1]) != e:
            raise WriteRefused("the answers file read back differs from the computed results; nothing was written")
        text = csv_text(e)
        if not text.isascii():
            raise WriteRefused("CSV text is not plain ASCII")
        pairs[1][1].write_text(text, encoding="ascii", newline="")
        if pairs[1][1].read_text(encoding="ascii") != text:
            raise WriteRefused("the CSV read back differs; nothing was written")
        for f, tmp in pairs:
            os.replace(tmp, f)
    finally:
        for _, tmp in pairs:
            tmp.unlink(missing_ok=True)
    return final
