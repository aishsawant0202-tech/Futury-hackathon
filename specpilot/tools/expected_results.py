"""Expected-results calculator (Station 6, level 1): for each dummy person, must the message be sent?

Reads the SQL dummy file written by Station 5 and the approved card, and answers per person and reading
from the person's RAW values only (report date, sick periods, insurance ...): every condition of the card
is recomputed with the fact dictionary, and the card's AND / OR logic gives the answer. The hidden design
labels in the dummy file (_truth, all_true) are NOT used here; Step 3 compares against them as a second
opinion.

Refuses to compute when:
    the card is not approved, was changed after approval, or cites placeholder rule ids
    the dummy file was made from another card version (fingerprint)
    the dummy file's own self-check did not pass, or the file cannot be read as a dummy file
"""
import hashlib
import sqlite3
from pathlib import Path

from specpilot.criteria import CriteriaCard
from specpilot.dummies import DummyFile
from specpilot.expected import VERSION, ConditionResult, ExpectedFile, ExpectedResult, reading_key
from specpilot.tools import facts
from specpilot.tools.branch_maker import reading_choices
from specpilot.tools.dummy_writer import load
from specpilot.tools.self_check import check_card


class ExpectedError(Exception):
    """The expected results cannot be computed; the message says why."""


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_meta(path: Path) -> dict[str, str]:
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            return dict(conn.execute("SELECT key, value FROM dummy_meta").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as err:
        raise ExpectedError(f"{Path(path).name} is not a dummy file: {err}") from None


def load_dummy(path: Path) -> DummyFile:
    path = Path(path)
    if not path.exists():
        raise ExpectedError(f"dummy file not found: {path}")
    meta = read_meta(path)
    if not meta.get("self_check", "").startswith("SELF-CHECK PASSED"):
        raise ExpectedError(f"{path.name}: its self-check did not pass ({meta.get('self_check', 'no record')})")
    try:
        return load(path)
    except Exception as err:  # broken tables or values: never compute answers from a damaged file
        raise ExpectedError(f"{path.name} cannot be read as a dummy file: {err}") from None


def check_inputs(card: CriteriaCard, d: DummyFile) -> None:
    problems = check_card(card)
    if problems:
        raise ExpectedError("; ".join(p.message for p in problems))
    if (d.card_id, d.card_version, d.card_hash) != (card.id, card.version, card.content_hash()):
        raise ExpectedError(f"the dummy file was made from {d.card_id} v{d.card_version}, not from the approved "
                            f"{card.id} v{card.version} (fingerprints differ)")


def compute(card: CriteriaCard, dummy_path: Path) -> ExpectedFile:
    """The expected result of every dummy person, for every reading of the card."""
    d = load_dummy(dummy_path)
    check_inputs(card, d)
    readings = reading_choices(card)
    results = []
    for p in d.people:
        values = facts.evaluate(d.scenario_of(p))  # raw values only; the labels are not looked at
        for reading in readings:
            conditions = [
                ConditionResult(criterion_id=cid, fact=c.fact, op=c.op, limit=c.value, value=values[c.fact],
                                met=facts.holds(values[c.fact], c.op, c.value), rule_ids=c.source_rule_ids,
                                quote=c.quote)
                for cid, c in card.conditions(reading).items()]
            met = [c.met for c in conditions]
            results.append(ExpectedResult(person_id=p.person_id, neutral_id=p.neutral_id, reading=reading,
                                          logic=card.logic, conditions=conditions,
                                          message_expected=all(met) if card.logic == "AND" else any(met)))
    return ExpectedFile(card_id=card.id, card_version=card.version, card_hash=card.content_hash(), seed=d.seed,
                        dummy_file=Path(dummy_path).name, dummy_sha256=sha256(dummy_path), logic=card.logic,
                        n_criteria=len(card.criteria), made_by=VERSION, person_ids=[p.person_id for p in d.people],
                        readings=readings, results=results)


# --- consistency check: the answers against the people's design labels -----------------------------------------
#
# The calculator works from the raw values only; every dummy person also carries the labels it was DESIGNED
# with (C1_truth ..., all_true). The two are independent, so they must agree. A disagreement means the
# dummy data or the calculator is wrong: it is reported person by person, condition by condition, and blocks
# writing. It is never resolved silently.
# A label describes the person under its own branch's reading (branch ids carry it: ...-R2-B07 = reading 2,
# no -R = the main reading). Results for other readings have no label to compare with; they are counted in
# a note.

import re  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from specpilot.tools.self_check import Problem  # noqa: E402

_READING = re.compile(r"-R(\d+)-B\d+")


def design_reading(branch_id: str, readings: list[dict[str, str]]) -> dict[str, str]:
    """The reading a branch was made for: the main one, or readings[k-1] for '...-Rk-B..'."""
    m = _READING.search(branch_id)
    index = int(m.group(1)) - 1 if m else 0
    if not 0 <= index < len(readings):
        raise ExpectedError(f"{branch_id} names reading {index + 1}, the card has {len(readings)}")
    return readings[index]


@dataclass
class Consistency:
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    compared: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        head = (f"CONSISTENCY {'PASSED' if self.ok else 'FAILED'}: {self.compared} answer(s) compared with the "
                f"design labels, {len(self.problems)} problem(s)")
        return "\n".join([head] + [f"  ✘ {p.text()}" for p in self.problems] + [f"  ⚑ {n}" for n in self.notes])


def check_consistency(e: ExpectedFile, d: DummyFile) -> Consistency:
    out = Consistency()
    if (e.card_id, e.card_version, e.card_hash, e.seed) != (d.card_id, d.card_version, d.card_hash, d.seed):
        out.problems.append(Problem("expected file", "origin", "the results were computed for another card version "
                                                               "or seed than this dummy file"))
        return out
    if e.person_ids != [p.person_id for p in d.people]:
        missing = sorted(set(p.person_id for p in d.people) - set(e.person_ids))
        extra = sorted(set(e.person_ids) - set(p.person_id for p in d.people))
        out.problems.append(Problem("expected file", "people", f"people differ from the dummy file: "
                                                               f"missing {missing[:3]}, unknown {extra[:3]}"
                                    if missing or extra else "people are in a different order than the dummy file"))
        return out
    by_key = {(r.person_id, reading_key(r.reading)): r for r in e.results}
    unlabelled = 0
    for p in d.people:
        mine = design_reading(p.branch_id, e.readings)
        for reading in e.readings:
            r = by_key[(p.person_id, reading_key(reading))]
            if reading != mine:
                unlabelled += 1
                continue
            out.compared += 1
            wrong = [c for c in r.conditions if int(c.met) != p.truth[c.criterion_id]]
            for c in wrong:
                out.problems.append(Problem(p.person_id, "condition",
                                            f"{c.criterion_id} {c.condition()}: designed to be "
                                            f"{'met' if p.truth[c.criterion_id] else 'failed'}, but from the raw values "
                                            f"{c.fact} = {c.value} -> {'met' if c.met else 'failed'}"))
            if r.message_expected != bool(p.all_true):
                out.problems.append(Problem(p.person_id, "message",
                                            f"expected result says the message {'must' if r.message_expected else 'must not'}"
                                            f" be sent, the design label all_true = {p.all_true} says the opposite"))
    if unlabelled:
        out.notes.append(f"{unlabelled} result(s) for other readings have no design label to compare with "
                         f"(each person is labelled for its own branch's reading only)")
    return out


# --- the command: dummy file + approved card -> checked answers -------------------------------------------------

import argparse  # noqa: E402
import sys  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    from specpilot.tools import expected_writer
    from specpilot.tools.branch_maker import BranchError, load_card

    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Work out the expected result of every dummy person (must the message "
                                            "be sent?), check it against the design labels, and write the answers "
                                            "to their own files.")
    p.add_argument("dummy", help="SQL dummy file (python -m specpilot.tools.dummy_writer)")
    p.add_argument("card", help="the approved precondition card (the dummy file's card, or its --background card)")
    p.add_argument("--content", metavar="CONTENT_CARD", help="level 2: work out the DBVO content instead, with this "
                                                            "approved content card (python -m specpilot.dbvo)")
    p.add_argument("--dry-run", action="store_true", help="compute and check everything, write nothing")
    p.add_argument("--out", default=str(expected_writer.OUT_DIR), help="folder for the answers (not the dummy folder)")
    p.add_argument("--explain", metavar="DUMMY-0001", help="print the full explanation for one person")
    a = p.parse_args(argv)

    dummy, out = Path(a.dummy), Path(a.out)
    if not a.dry_run and out.resolve() == dummy.resolve().parent:
        print("error: the answers must not be written into the dummy file's folder; choose another --out",
              file=sys.stderr)
        return 1
    if a.content:
        return _content_main(a, dummy, out)
    try:
        card = load_card(Path(a.card))
        e = compute(card, dummy)
        consistency = check_consistency(e, load_dummy(dummy))
    except (BranchError, ExpectedError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(e.summary())
    print(consistency.text())
    if a.explain:
        chosen = [r for r in e.results if r.neutral_id == a.explain]
        if not chosen:
            print(f"error: no person {a.explain} in {dummy.name}", file=sys.stderr)
            return 1
        print()
        print("\n\n".join(r.explanation() for r in chosen))
    if not consistency.ok:
        print("error: the expected results disagree with the design labels; nothing was written", file=sys.stderr)
        return 1
    if a.dry_run:
        print("Dry run passed: nothing was written.")
        return 0
    try:
        written = expected_writer.write_expected(e, consistency, out)
    except expected_writer.WriteRefused as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {written.sqlite}\n      {written.csv}")
    return 0


def _content_main(a, dummy: Path, out: Path) -> int:
    """Level 2: the DBVO content of every person and reading, checked and written to <dummy stem>.dbvo.*"""
    from pydantic import ValidationError

    from specpilot import dbvo
    from specpilot.tools import dbvo_writer
    from specpilot.tools.branch_maker import BranchError, load_card
    from specpilot.tools.dbvo_calc import ContentError, check_content, content_file
    try:
        content = dbvo.load_card(Path(a.content))
    except OSError as err:
        print(f"error: cannot read {a.content}: {err.strerror}", file=sys.stderr)
        return 1
    except ValidationError as err:
        print(f"error: {a.content} is not a valid content card: "
              f"{err.errors()[0]['msg'].removeprefix('Value error, ')}", file=sys.stderr)
        return 1
    try:
        precondition = load_card(Path(a.card))
        cf = content_file(content, precondition, dummy)
        checks = check_content(content, precondition, dummy, cf)
    except (BranchError, ExpectedError, ContentError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if content.approved_with_placeholders:
        print(f"⚑ content card {content.card_id} v{content.version} was approved accepting "
              f"{content.approved_with_placeholders} PLACEHOLDER source(s): these answers are provisional")
    print(cf.summary())
    print(checks.text().replace("CONSISTENCY", "CONTENT CHECKS", 1).replace("compared with the design labels",
                                                                             "checked against the content rules", 1))
    if a.explain:
        chosen = [r for r in cf.results if r.neutral_id == a.explain]
        if not chosen:
            print(f"error: no person {a.explain} in {dummy.name}", file=sys.stderr)
            return 1
        print()
        print("\n\n".join(r.explanation() for r in chosen))
    if not checks.ok:
        print("error: the DBVO content failed its checks; nothing was written", file=sys.stderr)
        return 1
    if a.dry_run:
        print("Dry run passed: nothing was written.")
        return 0
    try:
        written = dbvo_writer.write_content(cf, checks, out)
    except dbvo_writer.WriteRefused as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {written.sqlite}\n      {written.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
