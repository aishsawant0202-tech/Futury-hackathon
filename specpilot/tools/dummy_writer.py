"""Writes the checked dummy data to its SQL file: the end of the testing pipeline.

One SQLite file per card version and seed: output/dummies/<card>-v<version>-seed<seed>.sqlite

    dummy_people            one row per person: ids, raw values, hidden labels (C1_truth ..., all_true, edge)
    dummy_previous_periods  earlier sick periods (person_id, number 1 = oldest, start, end, certified)
    dummy_coverage          one row per branch, in branch order: person / impossible / unsolved (+ reason)
    dummy_meta              card, version, fingerprint, seed, logic, tool versions, self-check result

Dates are written as YYYY-MM-DD text, yes/no as 1/0. Nothing is written unless the self-check passed.
The file is written under a temporary name, read back and compared with the checked data, and only then
renamed: a half-written or unverified file never carries the real name. No timestamps: the same card and
seed give the same file.
"""
import os
import sqlite3
from pathlib import Path

from specpilot import config
from specpilot.dummies import Coverage, DummyFile, DummyPerson, PreviousPeriod
from specpilot.tools.self_check import Report

OUT_DIR = config.ROOT / "output" / "dummies"
VERSION = "dummy_writer 0.1"

RAW = ["report_date", "current_start", "current_end", "current_certified", "open_end", "statutory_insured",
       "marginal_employment"]
IDS = ["person_id", "neutral_id", "branch_id", "card_id", "card_version", "card_hash", "seed", "provenance", "made_by"]


class WriteRefused(Exception):
    """Nothing was written; the message says why."""


def file_name(d: DummyFile) -> str:
    return f"{d.card_id}-v{d.card_version}-seed{d.seed}.sqlite"


def _truth_columns(n: int) -> list[str]:
    return [f"C{i}_truth" for i in range(1, n + 1)]


def _schema(n: int) -> str:
    truth = ",\n    ".join(f"{c} INTEGER NOT NULL CHECK ({c} IN (0, 1))" for c in _truth_columns(n))
    yes_no = ",\n    ".join(f"{c} INTEGER NOT NULL CHECK ({c} IN (0, 1))" for c in RAW[3:])
    return f"""
CREATE TABLE dummy_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE dummy_people (
    person_id      TEXT PRIMARY KEY,
    neutral_id     TEXT NOT NULL UNIQUE,
    branch_id      TEXT NOT NULL,
    card_id        TEXT NOT NULL,
    card_version   INTEGER NOT NULL,
    card_hash      TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    provenance     TEXT NOT NULL CHECK (provenance = 'SYNTHETIC'),
    made_by        TEXT NOT NULL,
    report_date    TEXT NOT NULL,
    current_start  TEXT NOT NULL,
    current_end    TEXT NOT NULL,
    {yes_no},
    {truth},
    all_true       INTEGER NOT NULL CHECK (all_true IN (0, 1)),
    edge_criterion TEXT NOT NULL DEFAULT '',
    edge_value     INTEGER
);
CREATE TABLE dummy_previous_periods (
    person_id TEXT NOT NULL REFERENCES dummy_people(person_id),
    number    INTEGER NOT NULL CHECK (number BETWEEN 1 AND 4),
    start     TEXT NOT NULL,
    "end"     TEXT NOT NULL,
    certified INTEGER NOT NULL CHECK (certified IN (0, 1)),
    PRIMARY KEY (person_id, number)
);
CREATE TABLE dummy_coverage (
    branch_id TEXT PRIMARY KEY,
    position  INTEGER NOT NULL UNIQUE,
    outcome   TEXT NOT NULL CHECK (outcome IN ('person', 'impossible', 'unsolved')),
    person_id TEXT REFERENCES dummy_people(person_id),
    reason    TEXT NOT NULL DEFAULT ''
);
"""


def _person_row(p: DummyPerson) -> list:
    raw = [p.report_date.isoformat(), p.current_start.isoformat(), p.current_end.isoformat(),
           int(p.current_certified), int(p.open_end), int(p.statutory_insured), int(p.marginal_employment)]
    return [getattr(p, c) for c in IDS] + raw + list(p.truth.values()) + [p.all_true, p.edge_criterion, p.edge_value]


def _write(path: Path, d: DummyFile, report: Report) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_schema(d.n_criteria))
        meta = {"card_id": d.card_id, "card_version": str(d.card_version), "card_hash": d.card_hash,
                "seed": str(d.seed), "logic": d.logic, "n_criteria": str(d.n_criteria), "made_by": d.made_by,
                "written_by": VERSION, "provenance": "SYNTHETIC", "self_check": report.text().splitlines()[0],
                "notes": "\n".join(report.notes)}
        if d.background_card_id:
            meta |= {"background_card_id": d.background_card_id, "background_card_hash": d.background_card_hash}
        with conn:
            conn.executemany("INSERT INTO dummy_meta VALUES (?, ?)", sorted(meta.items()))
            cols = IDS + RAW + _truth_columns(d.n_criteria) + ["all_true", "edge_criterion", "edge_value"]
            conn.executemany(f"INSERT INTO dummy_people ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [_person_row(p) for p in d.people])
            conn.executemany('INSERT INTO dummy_previous_periods (person_id, number, start, "end", certified) '
                             "VALUES (?, ?, ?, ?, ?)",
                             [(q.person_id, q.number, q.start.isoformat(), q.end.isoformat(), int(q.certified))
                              for q in d.previous])
            conn.executemany("INSERT INTO dummy_coverage VALUES (?, ?, ?, ?, ?)",
                             [(c.branch_id, i, c.outcome, c.person_id or None, c.reason)
                              for i, c in enumerate(d.coverage, start=1)])
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise WriteRefused(f"foreign key problems in the written file: {bad[:3]}")
    finally:
        conn.close()


def load(path: Path) -> DummyFile:
    """Read a dummy file back from SQL into the checked form (used to verify every write)."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = dict(conn.execute("SELECT key, value FROM dummy_meta").fetchall())
        n = int(meta["n_criteria"])
        people = []
        for r in conn.execute("SELECT p.* FROM dummy_people p JOIN dummy_coverage c ON c.person_id = p.person_id "
                              "ORDER BY c.position"):
            row = dict(r)
            people.append(DummyPerson(
                **{c: row[c] for c in IDS}, **{c: row[c] for c in RAW[:3]},
                **{c: bool(row[c]) for c in RAW[3:]},
                truth={c.removesuffix("_truth"): row[c] for c in _truth_columns(n)},
                all_true=row["all_true"], edge_criterion=row["edge_criterion"], edge_value=row["edge_value"]))
        previous = [PreviousPeriod(person_id=r["person_id"], number=r["number"], start=r["start"], end=r["end"],
                                   certified=bool(r["certified"]))
                    for r in conn.execute('SELECT * FROM dummy_previous_periods ORDER BY person_id, number')]
        order = {p.person_id: i for i, p in enumerate(people)}
        previous.sort(key=lambda q: (order[q.person_id], q.number))
        coverage = [Coverage(branch_id=r["branch_id"], outcome=r["outcome"], person_id=r["person_id"] or "",
                             reason=r["reason"])
                    for r in conn.execute("SELECT * FROM dummy_coverage ORDER BY position")]
    finally:
        conn.close()
    return DummyFile(card_id=meta["card_id"], card_version=int(meta["card_version"]), card_hash=meta["card_hash"],
                     seed=int(meta["seed"]), logic=meta["logic"], n_criteria=n, made_by=meta["made_by"],
                     people=people, previous=previous, coverage=coverage,
                     background_card_id=meta.get("background_card_id", ""),
                     background_card_hash=meta.get("background_card_hash", ""))


def write_sqlite(d: DummyFile, report: Report, out_dir: Path = OUT_DIR) -> Path:
    """Write the dummy file, only if the self-check passed. Returns the path of the SQLite file."""
    if not report.ok:
        raise WriteRefused(f"the self-check found {len(report.problems)} problem(s); nothing was written:\n"
                           + "\n".join(f"  {p.text()}" for p in report.problems[:20]))
    if report.people_checked != len(d.people):
        raise WriteRefused(f"the self-check covered {report.people_checked} people, the file has {len(d.people)}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / file_name(d)
    tmp = out_dir / f".{final.name}.tmp"
    tmp.unlink(missing_ok=True)
    try:
        _write(tmp, d, report)
        if load(tmp) != d:
            raise WriteRefused("the file read back differs from the checked data; nothing was written")
        os.replace(tmp, final)  # atomic: the real name only ever points to a complete, verified file
    finally:
        tmp.unlink(missing_ok=True)
    return final


# --- CSV exports ----------------------------------------------------------------------------------------------
#
#   <name>.full.csv      everything, one row per person: ids, raw values, earlier periods flattened into
#                        prev1_... prev4_... columns, hidden labels. For the testing team.
#   <name>.stripped.csv  raw values only, neutral ids, sorted by neutral id: no person/branch ids, no labels,
#                        nothing that reveals which conditions a person was designed to meet. For the software
#                        developers. provenance=SYNTHETIC stays on every row.
# Yes/no are 1/0 and dates YYYY-MM-DD, as in the SQL file. Plain ASCII, "\n" line ends, no timestamps.

import csv  # noqa: E402
import io  # noqa: E402
from dataclasses import dataclass  # noqa: E402

PREV_COLUMNS = [f"prev{i}_{part}" for i in range(1, 5) for part in ("start", "end", "certified")]
STRIPPED_COLUMNS = ["neutral_id", "provenance", "card_id", "card_version"] + RAW + ["n_previous"] + PREV_COLUMNS


def full_columns(n: int) -> list[str]:
    return (["neutral_id"] + [c for c in IDS if c != "neutral_id"] + RAW + ["n_previous"] + PREV_COLUMNS
            + _truth_columns(n) + ["all_true", "edge_criterion", "edge_value"])


def _row(d: DummyFile, p: DummyPerson) -> dict:
    earlier = sorted((q for q in d.previous if q.person_id == p.person_id), key=lambda q: q.number)
    row = {c: getattr(p, c) for c in IDS}
    row |= {"report_date": p.report_date.isoformat(), "current_start": p.current_start.isoformat(),
            "current_end": p.current_end.isoformat(), **{c: int(getattr(p, c)) for c in RAW[3:]},
            "n_previous": len(earlier)}
    for i in range(1, 5):
        q = earlier[i - 1] if i <= len(earlier) else None
        row |= {f"prev{i}_start": q.start.isoformat() if q else "", f"prev{i}_end": q.end.isoformat() if q else "",
                f"prev{i}_certified": int(q.certified) if q else ""}
    row |= {f"{k}_truth": v for k, v in p.truth.items()}
    row |= {"all_true": p.all_true, "edge_criterion": p.edge_criterion,
            "edge_value": "" if p.edge_value is None else p.edge_value}
    return row


def csv_text(d: DummyFile, stripped: bool) -> str:
    columns = STRIPPED_COLUMNS if stripped else full_columns(d.n_criteria)
    people = sorted(d.people, key=lambda p: p.neutral_id) if stripped else d.people
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(_row(d, p) for p in people)
    return buf.getvalue()


@dataclass(frozen=True)
class Outputs:
    sqlite: Path
    full_csv: Path
    stripped_csv: Path


def output_paths(d: DummyFile, out_dir: Path = OUT_DIR) -> Outputs:
    stem = Path(out_dir) / file_name(d).removesuffix(".sqlite")
    return Outputs(Path(f"{stem}.sqlite"), Path(f"{stem}.full.csv"), Path(f"{stem}.stripped.csv"))


def _check_stripped(text: str, d: DummyFile) -> None:
    """The stripped file must not give away anything about the design."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if (list(rows[0]) if rows else []) != STRIPPED_COLUMNS:
        raise WriteRefused("stripped CSV has unexpected columns")
    leaks = [x for p in d.people for x in (p.person_id, p.branch_id) if x in text]
    if leaks or "_truth" in text or "all_true" in text:
        raise WriteRefused(f"stripped CSV would reveal the design: {leaks[:3] or 'label columns'}")
    if [r["neutral_id"] for r in rows] != sorted(p.neutral_id for p in d.people):
        raise WriteRefused("stripped CSV must list every person once, sorted by neutral id")


def write_outputs(d: DummyFile, report: Report, out_dir: Path = OUT_DIR) -> Outputs:
    """The SQL file and both CSVs, written as one unit: all to temporary files, all verified, then all renamed.
    Only if the self-check passed."""
    if not report.ok:
        raise WriteRefused(f"the self-check found {len(report.problems)} problem(s); nothing was written:\n"
                           + "\n".join(f"  {p.text()}" for p in report.problems[:20]))
    if report.people_checked != len(d.people):
        raise WriteRefused(f"the self-check covered {report.people_checked} people, the file has {len(d.people)}")
    final = output_paths(d, out_dir)
    final.sqlite.parent.mkdir(parents=True, exist_ok=True)
    pairs = [(f, f.parent / f".{f.name}.tmp") for f in (final.sqlite, final.full_csv, final.stripped_csv)]
    for _, tmp in pairs:
        tmp.unlink(missing_ok=True)
    try:
        _write(pairs[0][1], d, report)
        if load(pairs[0][1]) != d:
            raise WriteRefused("the SQL file read back differs from the checked data; nothing was written")
        full, stripped = csv_text(d, stripped=False), csv_text(d, stripped=True)
        if not (full.isascii() and stripped.isascii()):
            raise WriteRefused("CSV text is not plain ASCII")
        _check_stripped(stripped, d)
        pairs[1][1].write_text(full, encoding="ascii", newline="")
        pairs[2][1].write_text(stripped, encoding="ascii", newline="")
        if pairs[1][1].read_text(encoding="ascii") != full or pairs[2][1].read_text(encoding="ascii") != stripped:
            raise WriteRefused("a CSV file read back differs; nothing was written")
        for f, tmp in pairs:
            os.replace(tmp, f)
    finally:
        for _, tmp in pairs:
            tmp.unlink(missing_ok=True)
    return final


# --- the command: approved card + branches + seed -> checked dummy files ----------------------------------------

import argparse  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402


def coverage_summary(d: DummyFile) -> str:
    edges = sum(1 for c in d.coverage if "-E" in c.branch_id.rsplit("-B", 1)[-1])
    outcomes = {o: sum(c.outcome == o for c in d.coverage) for o in ("person", "impossible", "unsolved")}
    return (f"{d.card_id} v{d.card_version}: {len(d.coverage) - edges} branches + {edges} edge branch(es) -> "
            f"{outcomes['person']} people, {outcomes['impossible']} impossible, {outcomes['unsolved']} unsolved "
            f"· seed {d.seed}")


def main(argv: list[str] | None = None) -> int:
    from specpilot.dummies import build
    from specpilot.tools import branch_maker, value_finder
    from specpilot.tools.self_check import check

    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Make the dummy people for an approved card, check every one, and write "
                                            "the SQL dummy file (+ full and stripped CSV). The end of the testing "
                                            "pipeline.")
    p.add_argument("card", help="approved card JSON (python -m specpilot.review)")
    p.add_argument("branches", help="branches JSON of that card (python -m specpilot.tools.branch_maker)")
    p.add_argument("--seed", type=int, help="same seed = same files; a random one is chosen and shown if left out")
    p.add_argument("--dry-run", action="store_true", help="make and check everything, write nothing")
    p.add_argument("--out", default=str(OUT_DIR), help="folder for the dummy files")
    p.add_argument("--timeout-ms", type=int, default=value_finder.TIMEOUT_MS)
    p.add_argument("--background", help="the approved background card the branches were made with (--background "
                                        "of the branch maker)")
    a = p.parse_args(argv)
    seed = a.seed if a.seed is not None else secrets.randbelow(1_000_000)

    try:
        card = branch_maker.load_card(Path(a.card))
        bs = value_finder.load_branches(Path(a.branches))
        background = branch_maker.load_card(Path(a.background)) if a.background else None
    except (branch_maker.BranchError, value_finder.InputError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if (bs.card_id, bs.card_version, bs.card_hash) != (card.id, card.version, card.content_hash()):
        print(f"error: {a.branches} was made from {bs.card_id} v{bs.card_version}, not from the approved "
              f"{card.id} v{card.version}; make the branches again", file=sys.stderr)
        return 1

    if bs.background_card_id and (background is None or background.content_hash() != bs.background_card_hash):
        print(f"error: the branches were made with background card {bs.background_card_id}; pass that exact approved "
              f"card with --background", file=sys.stderr)
        return 1
    if background is not None and not bs.background_card_id:
        print("error: --background was given, but the branches have no background card", file=sys.stderr)
        return 1
    print(f"Seed {seed}" + ("" if a.seed is not None else f" (repeat with --seed {seed})")
          + (" - DRY RUN: nothing will be written" if a.dry_run else ""))
    people = value_finder.make_people(bs, seed, a.timeout_ms)
    d = build(people, bs, made_by=f"{value_finder.VERSION}; {branch_maker.VERSION}")
    report = check(card, bs, d, background)
    print(report.text())
    print(coverage_summary(d))
    if not report.ok:
        print("error: the self-check failed; nothing was written", file=sys.stderr)
        return 1
    if a.dry_run:
        print("Dry run passed: nothing was written.")
        return 0
    try:
        out = write_outputs(d, report, Path(a.out))
    except WriteRefused as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {out.sqlite}\n      {out.full_csv}\n      {out.stripped_csv}  (for the software developers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
