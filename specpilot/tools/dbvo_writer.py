"""Writes the DBVO content answers to their own files, next to the level 1 answers (never next to the dummies).

    output/expected/<dummy file stem>.dbvo.sqlite
        dbvo_meta     content card (version, fingerprint, accepted placeholders), precondition card, the dummy file
                      and its fingerprint, seed, readings, check result
        dbvo_results  one row per person and reading: message (1/0), number of reported periods, why the chain stopped
        dbvo_fields   one row per person, reading and field: ABGABEGRUND, AU-AB-AG, ANZAHL-AU, BEGINN-AU-n, ENDE-AU-n
        dbvo_periods  one row per reported period: n, start, end, why it is in the chain
        dbvo_skipped  absences left out, with the reason
    output/expected/<dummy file stem>.dbvo.csv
        one row per person and reading: the message flag and every field, ready to compare with a DBVO record

Written only if the content checks passed, under temporary names, read back and compared, then renamed together.
No timestamps: the same inputs give the same files.
"""
import csv
import io
import json
import os
import sqlite3
from pathlib import Path

from specpilot.dbvo import ContentFile, ContentResult, ReportedPeriod
from specpilot.tools.expected_writer import OUT_DIR, Outputs

VERSION = "dbvo_writer 0.1"


class WriteRefused(Exception):
    """Nothing was written; the message says why."""


def output_paths(cf: ContentFile, out_dir: Path = OUT_DIR) -> Outputs:
    stem = Path(out_dir) / Path(cf.dummy_file).name.removesuffix(".sqlite")
    return Outputs(Path(f"{stem}.dbvo.sqlite"), Path(f"{stem}.dbvo.csv"))


SCHEMA = """
CREATE TABLE dbvo_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE dbvo_results (
    person_id TEXT NOT NULL, neutral_id TEXT NOT NULL, reading TEXT NOT NULL, position INTEGER NOT NULL UNIQUE,
    message_expected INTEGER NOT NULL CHECK (message_expected IN (0, 1)), periods INTEGER NOT NULL,
    stop_reason TEXT NOT NULL, PRIMARY KEY (person_id, reading));
CREATE TABLE dbvo_fields (
    person_id TEXT NOT NULL, reading TEXT NOT NULL, position INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (person_id, reading, field),
    FOREIGN KEY (person_id, reading) REFERENCES dbvo_results (person_id, reading));
CREATE TABLE dbvo_periods (
    person_id TEXT NOT NULL, reading TEXT NOT NULL, n INTEGER NOT NULL, start TEXT NOT NULL, "end" TEXT NOT NULL,
    why TEXT NOT NULL, PRIMARY KEY (person_id, reading, n),
    FOREIGN KEY (person_id, reading) REFERENCES dbvo_results (person_id, reading));
CREATE TABLE dbvo_skipped (
    person_id TEXT NOT NULL, reading TEXT NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (person_id, reading, position),
    FOREIGN KEY (person_id, reading) REFERENCES dbvo_results (person_id, reading));
"""


def _write_sqlite(path: Path, cf: ContentFile, check_line: str, notes: list[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        meta = {k: str(v) for k, v in cf.model_dump(exclude={"results", "readings", "person_ids"}).items()}
        meta |= {"readings": json.dumps(cf.readings), "person_ids": json.dumps(cf.person_ids), "written_by": VERSION,
                 "checks": check_line, "notes": "\n".join(notes)}
        with conn:
            conn.executemany("INSERT INTO dbvo_meta VALUES (?, ?)", sorted(meta.items()))
            for i, r in enumerate(cf.results, start=1):
                key = (r.person_id, r.reading)
                conn.execute("INSERT INTO dbvo_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (*key[:1], r.neutral_id, r.reading, i, int(r.message_expected), len(r.periods), r.stop_reason))
                conn.executemany("INSERT INTO dbvo_fields VALUES (?, ?, ?, ?, ?)",
                                 [(*key, j, f, v) for j, (f, v) in enumerate(r.fields.items(), start=1)])
                conn.executemany("INSERT INTO dbvo_periods VALUES (?, ?, ?, ?, ?, ?)",
                                 [(*key, p.n, p.start.isoformat(), p.end.isoformat(), p.why) for p in r.periods])
                conn.executemany("INSERT INTO dbvo_skipped VALUES (?, ?, ?, ?)",
                                 [(*key, j, t) for j, t in enumerate(r.skipped, start=1)])
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise WriteRefused("broken links in the written content file")
    finally:
        conn.close()


def load(path: Path) -> ContentFile:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = dict(conn.execute("SELECT key, value FROM dbvo_meta").fetchall())

        def grouped(sql):
            out: dict = {}
            for row in conn.execute(sql):
                out.setdefault((row["person_id"], row["reading"]), []).append(row)
            return out

        fields = grouped("SELECT * FROM dbvo_fields ORDER BY person_id, reading, position")
        periods = grouped("SELECT * FROM dbvo_periods ORDER BY person_id, reading, n")
        skipped = grouped("SELECT * FROM dbvo_skipped ORDER BY person_id, reading, position")
        results = []
        for r in conn.execute("SELECT * FROM dbvo_results ORDER BY position"):
            key = (r["person_id"], r["reading"])
            results.append(ContentResult(
                person_id=r["person_id"], neutral_id=r["neutral_id"], reading=r["reading"],
                message_expected=bool(r["message_expected"]),
                fields={f["field"]: f["value"] for f in fields.get(key, [])},
                periods=[ReportedPeriod(n=p["n"], start=p["start"], end=p["end"], why=p["why"]) for p in periods.get(key, [])],
                skipped=[s["text"] for s in skipped.get(key, [])], stop_reason=r["stop_reason"]))
    finally:
        conn.close()
    return ContentFile(content_card_id=meta["content_card_id"], content_card_version=int(meta["content_card_version"]),
                       content_card_hash=meta["content_card_hash"],
                       approved_with_placeholders=int(meta["approved_with_placeholders"]),
                       precondition_card_id=meta["precondition_card_id"],
                       precondition_card_hash=meta["precondition_card_hash"], dummy_file=meta["dummy_file"],
                       dummy_sha256=meta["dummy_sha256"], seed=int(meta["seed"]), readings=json.loads(meta["readings"]),
                       person_ids=json.loads(meta["person_ids"]), results=results, made_by=meta["made_by"])


def csv_text(cf: ContentFile) -> str:
    most = max((len(r.periods) for r in cf.results), default=0)
    columns = (["neutral_id", "person_id", "reading", "message_expected", "ABGABEGRUND", "AU-AB-AG", "ANZAHL-AU"]
               + [f"{x}-AU-{n}" for n in range(1, most + 1) for x in ("BEGINN", "ENDE")] + ["stop_reason", "dummy_file"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="raise")
    w.writeheader()
    for r in cf.results:
        w.writerow({"neutral_id": r.neutral_id, "person_id": r.person_id, "reading": r.reading,
                    "message_expected": int(r.message_expected), **r.fields, "stop_reason": r.stop_reason,
                    "dummy_file": cf.dummy_file})
    return buf.getvalue()


def write_content(cf: ContentFile, checks, out_dir: Path = OUT_DIR) -> Outputs:
    """Write the content answers, only if every check passed and covered every result."""
    if not checks.ok:
        raise WriteRefused(f"the content checks found {len(checks.problems)} problem(s); nothing was written:\n"
                           + "\n".join(f"  {p.text()}" for p in checks.problems[:20]))
    if checks.compared != len(cf.results):
        raise WriteRefused(f"the checks covered {checks.compared} result(s), the file has {len(cf.results)}")
    final = output_paths(cf, out_dir)
    final.sqlite.parent.mkdir(parents=True, exist_ok=True)
    pairs = [(f, f.parent / f".{f.name}.tmp") for f in (final.sqlite, final.csv)]
    for _, tmp in pairs:
        tmp.unlink(missing_ok=True)
    try:
        line = f"CONTENT CHECKS PASSED: {checks.compared} result(s), 0 problem(s)"
        _write_sqlite(pairs[0][1], cf, line, checks.notes)
        if load(pairs[0][1]) != cf:
            raise WriteRefused("the content file read back differs from the computed results; nothing was written")
        text = csv_text(cf)
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
