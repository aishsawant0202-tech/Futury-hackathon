"""Branch maker: every met / not-met combination of a criteria card's conditions. Plain code, no model.

For a card with n conditions this gives 2^n branches per reading, in binary order with C1 changing
slowest: B01 = every condition met ... last = no condition met. A card whose conditions can be read two
ways (alternatives) gets a full 2^n set for every combination of readings; the main reading has no
prefix, the others are numbered R2, R3, ...

    card with 5 conditions, 1 reading        -> 32 branches
    card with 5 conditions, C3 has 2 readings -> 2 x 32 = 64 branches

The result is a BranchSet, whose own checks (specpilot/branches.py) guarantee that all 2^n
combinations are there exactly once.

Size: 2^n doubles with every condition. From WARN_FROM conditions a warning is given; from
REFUSE_FROM (or more than MAX_BRANCHES in total) the maker refuses unless force=True.
"""
import argparse
import csv
import json
import sys
from functools import lru_cache
from itertools import product
from pathlib import Path

from pydantic import ValidationError

from specpilot import config
from specpilot.branches import Branch, BranchSet, CriterionTarget, Status, Target, all_true_of, json_key, kind_of
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.tools import facts, gold
from specpilot.tools.facts import FactType

OUT_DIR = config.ROOT / "output" / "branches"

VERSION = "branch_maker 0.1"
WARN_FROM = 10  # conditions: 1,024 branches per reading
REFUSE_FROM = 12  # conditions: 4,096 branches per reading
MAX_BRANCHES = 4096  # in total, all readings together


class BranchError(Exception):
    """The card cannot be branched with these settings; the message says why."""


def reading_choices(card: CriteriaCard) -> list[dict[str, str]]:
    """Every combination of readings. {} = all main readings; otherwise only the non-main choices."""
    ambiguous = card.readings()  # {C id: [main label, alternative labels...]}
    if not ambiguous:
        return [{}]
    cids = sorted(ambiguous, key=lambda c: int(c[1:]))
    out = []
    for labels in product(*(ambiguous[c] for c in cids)):
        out.append({c: label for c, label in zip(cids, labels) if label != ambiguous[c][0]})
    return out


def size_warnings(card: CriteriaCard) -> list[str]:
    n, r = len(card.criteria), len(reading_choices(card))
    total = r * 2 ** n
    out = []
    if n >= WARN_FROM:
        out.append(f"{n} conditions give 2^{n} = {2 ** n:,} branches per reading; check every condition is needed")
    if r > 1:
        out.append(f"{r} reading combinations x {2 ** n:,} = {total:,} branches; resolving ambiguities in review "
                   f"would reduce this")
    return out


def free_facts(card: CriteriaCard, background: CriteriaCard | None = None) -> list[str]:
    """Facts in the dictionary that no condition of the card is about.
    (Alternative readings are always about the same fact as their main reading; criteria.py enforces it.)"""
    used = {c.fact for c in card.criteria} | ({c.fact for c in background.criteria} if background else set())
    return [name for name in facts.FACTS if name not in used]


# --- impossible branches ----------------------------------------------------------------------------
#
# A branch is impossible when, for some fact, no value can do what all of the branch's targets on that
# fact demand at once: "total_days_12m >= 30 must FAIL" and "total_days_12m <= 60 must FAIL" would need
# fewer than 30 and more than 60 days. One target alone can be impossible too: "total_days_12m >= 1 must
# FAIL" needs 0 days, but the current absence always counts at least 1.
# Every value of the fact is tried: yes/no, or every whole number in its realistic range.
# Only conditions on the SAME fact are compared here. Clashes between different facts that share raw
# inputs (dates) are for the value finder (Station 4), which can prove them with a solver.

def _domain(fact: str) -> range | tuple[bool, bool]:
    f = facts.get(fact)
    if f.type is FactType.BOOLEAN:
        return (False, True)
    lo, hi = f.plausible
    return range(lo, hi + 1)


@lru_cache(maxsize=None)
def _satisfiable(fact: str, demands: tuple[tuple[str, str, bool], ...]) -> bool:
    """Is there a value of `fact` for which every (op, value, must_hold) demand comes out right?"""
    parsed = [(op, json.loads(value), must) for op, value, must in demands]
    return any(all(facts.holds(v, op, value) == must for op, value, must in parsed) for v in _domain(fact))


def _range_text(fact: str) -> str:
    f = facts.get(fact)
    return "" if f.type is FactType.BOOLEAN else f" in the realistic range {f.plausible[0]}-{f.plausible[1]} {f.unit}"


def impossible_reason(targets: list[CriterionTarget]) -> str:
    """Why these targets cannot all be reached, or "" if they can (as far as same-fact checks go)."""
    by_fact: dict[str, list[CriterionTarget]] = {}
    for t in targets:
        by_fact.setdefault(t.fact, []).append(t)
    reasons = []
    for fact, ts in by_fact.items():
        demands = tuple((t.op, json.dumps(t.value), t.target is Target.MET) for t in ts)
        if not _satisfiable(fact, demands):
            wanted = " and ".join(f"{t.criterion_id} ({t.describe()}) must be {t.target.value.upper()}" for t in ts)
            reasons.append(f"{wanted}: no {fact} value{_range_text(fact)} does {'that' if len(ts) == 1 else 'all of that'}")
    return "; ".join(reasons)


def mark_impossible(branch_set: BranchSet) -> BranchSet:
    """The same set with every impossible branch marked and explained. Nothing is removed: still 2^n."""
    branches = []
    for b in branch_set.branches:
        reason = impossible_reason(b.targets + b.background)
        status = Status.IMPOSSIBLE if reason else Status.POSSIBLE
        branches.append(b.model_copy(update={"status": status, "reason": reason}))
    return BranchSet.model_validate(branch_set.model_dump() | {"branches": [b.model_dump() for b in branches]})


# --- edge branches -----------------------------------------------------------------------------------------
#
# Bugs hide at limits: software that counts "more than 30" instead of "at least 30" passes every test
# except the one with exactly 30. An edge branch copies a branch and pins one number condition exactly at
# its limit:
#   on the all-met branch       every number condition at the LAST value that still meets it  (>= 30 -> 30)
#   on a single-fail branch     the failed number condition at the FIRST value that fails it  (>= 30 -> 29)
# Conditions with two limits get one edge branch per limit (between [30, 60] met -> 30 and 60;
# = 30 failed -> 29 and 31). Values outside the realistic range are skipped, and so is a value that
# would break another condition on the same fact in that branch.

@lru_cache(maxsize=None)
def _edge_values(fact: str, op: str, value: str, must: bool) -> tuple[int, ...]:
    v = json.loads(value)
    return tuple(x for x in _domain(fact)
                 if facts.holds(x, op, v) == must
                 and (facts.holds(x - 1, op, v) != must or facts.holds(x + 1, op, v) != must))


def edge_values(t: CriterionTarget) -> list[int]:
    """Values of t's fact right at the limit of t, on the side t's target asks for."""
    if facts.get(t.fact).type is not FactType.INTEGER:
        return []
    return list(_edge_values(t.fact, t.op, json.dumps(t.value), t.target is Target.MET))


def _edges_of(b: Branch) -> list[Branch]:
    if b.status is not Status.POSSIBLE or b.is_edge:
        return []
    if b.kind.value == "all_met":
        pinned = list(b.targets)
    elif b.kind.value == "single_fail":
        pinned = [t for t in b.targets if t.target is Target.FAILED]
    else:
        return []
    out: list[Branch] = []
    for t in pinned:
        others = [o for o in b.targets if o.fact == t.fact and o.criterion_id != t.criterion_id]
        for v in edge_values(t):
            if not all(facts.holds(v, o.op, o.value) == (o.target is Target.MET) for o in others):
                continue
            targets = [x.model_copy(update={"at_edge": True, "edge_value": v}) if x.criterion_id == t.criterion_id else x
                       for x in b.targets]
            out.append(Branch.model_validate(b.model_dump() | {
                "branch_id": f"{b.branch_id}-E{len(out) + 1}", "edge_of": b.branch_id,
                "targets": [x.model_dump() for x in targets]}))
    return out


def add_edges(branch_set: BranchSet) -> BranchSet:
    """The same set with edge branches added right after the branch each one is derived from."""
    branches: list[Branch] = []
    for b in branch_set.branches:
        branches.append(b)
        branches += _edges_of(b)
    return BranchSet.model_validate(branch_set.model_dump() | {"branches": [b.model_dump() for b in branches]})


# --- making the branches -------------------------------------------------------------------------------

def background_targets(background: CriteriaCard) -> list[CriterionTarget]:
    """The background card's conditions as targets every person must meet (its main reading only)."""
    if background.readings():
        raise BranchError(f"background card {background.id} still has open readings {background.readings()}; "
                          f"resolve them in review first")
    return [CriterionTarget(criterion_id=c.id, fact=c.fact, op=c.op, value=c.value, target=Target.MET)
            for c in background.criteria]


def make_branches(card: CriteriaCard, force: bool = False, check_possible: bool = True,
                  edges: bool = False, background: CriteriaCard | None = None) -> BranchSet:
    n = len(card.criteria)
    choices = reading_choices(card)
    total = len(choices) * 2 ** n
    if not force and (n >= REFUSE_FROM or total > MAX_BRANCHES):
        raise BranchError(f"{card.id}: {n} conditions x {len(choices)} reading(s) = {total:,} branches, above the "
                          f"limit ({MAX_BRANCHES:,} in total, fewer than {REFUSE_FROM} conditions). "
                          f"Reduce the card in review, or pass force=True / --force")
    free = free_facts(card, background)
    bg = background_targets(background) if background else []
    if bg and {t.fact for t in bg} & {c.fact for c in card.criteria}:
        raise BranchError(f"{card.id} and its background card {background.id} constrain the same fact(s) "
                          f"{sorted({t.fact for t in bg} & {c.fact for c in card.criteria})}")
    branches: list[Branch] = []
    for r, choice in enumerate(choices, start=1):
        prefix = "" if r == 1 else f"-R{r}"
        conditions = card.conditions(choice)
        for number, pattern in enumerate(product((Target.MET, Target.FAILED), repeat=n), start=1):
            targets = list(pattern)
            branches.append(Branch(
                branch_id=f"{card.id}{prefix}-B{number:0{max(2, len(str(2 ** n)))}d}",
                number=number,
                card_id=card.id,
                card_version=card.version,
                card_hash=card.content_hash(),
                logic=card.logic,
                reading=choice,
                targets=[{"criterion_id": cid, "fact": cond.fact, "op": cond.op, "value": cond.value,
                          "target": t.value} for (cid, cond), t in zip(conditions.items(), targets)],
                all_true=all_true_of(card.logic, targets),
                kind=kind_of(targets),
                free_facts=free,
                background=bg,
            ))
    result = BranchSet(card_id=card.id, card_version=card.version, card_hash=card.content_hash(), logic=card.logic,
                       n_criteria=n, readings=choices, branches=branches, made_by=VERSION,
                       background_card_id=background.id if background else "",
                       background_card_hash=background.content_hash() if background else "")
    if check_possible:
        result = mark_impossible(result)
    return add_edges(result) if edges else result


# --- reading cards, writing branches, command line ------------------------------------------------------

def load_card(path: Path, preview: bool = False) -> CriteriaCard:
    """Read a card file. Only an approved, unchanged card may be branched for real;
    with preview=True any valid card is accepted (nothing is written for it)."""
    try:
        card = CriteriaCard.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except OSError as err:
        raise BranchError(f"cannot read {path}: {err.strerror}") from None
    except ValidationError as err:
        first = err.errors()[0]["msg"].removeprefix("Value error, ")
        raise BranchError(f"{path} is not a valid criteria card: {first}") from None
    if preview:
        return card
    if card.status is not CardStatus.APPROVED:
        raise BranchError(f"{card.id} v{card.version} is {card.status.value}, not approved: approve it with "
                          f"python -m specpilot.review (or look at it with --preview)")
    if not card.approval_valid:
        raise BranchError(f"{card.id}: the approval does not match the content; review it again")
    placeholders = gold.placeholder_ids(card)
    if placeholders:
        raise BranchError(f"{card.id} cites placeholder rule ids {placeholders}; it cannot be branched")
    return card


CSV_HEAD = ["branch_id", "number", "reading", "pattern", "kind", "all_true", "status", "reason", "edge_of"]
CSV_TAIL = ["background", "free_facts", "card_id", "card_version", "card_hash", "made_by"]


def csv_rows(s: BranchSet) -> tuple[list[str], list[dict]]:
    """One row per branch; three columns per condition: what it says, met/failed, the pinned edge value.
    pattern uses letters (M = met, F = failed): Excel would turn "00110" into the number 110."""
    per_criterion = [f"C{i}_{part}" for i in range(1, s.n_criteria + 1) for part in ("condition", "target", "edge_value")]
    rows = []
    for b in s.branches:
        row = {"branch_id": b.branch_id, "number": b.number, "reading": json_key(b.reading) or "main",
               "pattern": "".join("M" if t.target is Target.MET else "F" for t in b.targets), "kind": b.kind.value,
               "all_true": int(b.all_true), "status": b.status.value, "reason": b.reason, "edge_of": b.edge_of,
               "free_facts": ";".join(b.free_facts), "card_id": b.card_id, "card_version": b.card_version,
               "card_hash": b.card_hash, "made_by": s.made_by,
               "background": "; ".join(t.describe() for t in b.background)}
        for t in b.targets:
            row[f"{t.criterion_id}_condition"] = t.describe()
            row[f"{t.criterion_id}_target"] = t.target.value
            row[f"{t.criterion_id}_edge_value"] = "" if t.edge_value is None else t.edge_value
        rows.append(row)
    return CSV_HEAD + per_criterion + CSV_TAIL, rows


def write_branches(s: BranchSet, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{s.card_id}-v{s.card_version}.branches"
    json_path, csv_path = out_dir / f"{stem}.json", out_dir / f"{stem}.csv"
    json_path.write_text(s.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    columns, rows = csv_rows(s)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return json_path, csv_path


def preview(s: BranchSet, limit: int = 40) -> str:
    lines = [s.summary(), ""]
    header = "  " + " ".join(f"C{i}" for i in range(1, s.n_criteria + 1))
    lines.append(f"  {'branch':<34} {'met?':<{max(4, s.n_criteria)}}  all_true  kind          status")
    for b in s.branches[:limit]:
        edge = next((f"  {t.criterion_id} pinned at {t.edge_value}" for t in b.targets if t.at_edge), "")
        lines.append(f"  {b.branch_id:<34} {b.pattern:<{max(4, s.n_criteria)}}  {int(b.all_true):<8}  {b.kind.value:<12}  "
                     f"{b.status.value}{edge}")
    if len(s.branches) > limit:
        lines.append(f"  ... {len(s.branches) - limit} more (--limit {len(s.branches)} shows all)")
    impossible = [b for b in s.branches if b.status is Status.IMPOSSIBLE]
    if impossible:
        lines.append("\n  Impossible:")
        lines += [f"  ✘ {b.branch_id}: {b.reason}" for b in impossible]
    lines.append(f"\n  Conditions ({header.strip()}):")
    for t in s.branches[0].targets:
        lines.append(f"    {t.criterion_id}: {t.describe()}")
    if s.background_card_id:
        lines.append(f"  Background (EVERY person meets these, from {s.background_card_id}):")
        lines += [f"    {t.criterion_id}: {t.describe()}" for t in s.branches[0].background]
    if s.branches[0].free_facts:
        lines.append(f"  Free facts (not on the card, varied by the value finder): {', '.join(s.branches[0].free_facts)}")
    return "\n".join(lines)


def _check_up_to_date(db_path: str, cards: list[CriteriaCard]) -> None:
    """Refuse a card that is not in the card store, or whose rules changed since it was stored."""
    from specpilot import card_store, db
    conn = db.connect(db_path)
    for c in cards:
        try:
            changed = card_store.out_of_date(conn, c)
        except card_store.StoreError as err:
            raise BranchError(str(err)) from None
        if changed:
            raise BranchError(f"{c.id} v{c.version} is out of date: rule(s) {', '.join(changed)} since it was "
                              f"approved; translate and review it again")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Make every met / not-met combination (2^n branches) of an approved card.")
    p.add_argument("card", help="approved card JSON (from python -m specpilot.review)")
    p.add_argument("--edges", action="store_true", help="add branches pinned exactly at number limits (30 / 29 days)")
    p.add_argument("--force", action="store_true", help=f"allow more than {MAX_BRANCHES:,} branches")
    p.add_argument("--out", default=str(OUT_DIR), help="folder for the CSV and JSON files")
    p.add_argument("--preview", action="store_true", help="show the branches of any card (also drafts), write nothing")
    p.add_argument("--limit", type=int, default=40, help="rows to show on screen")
    p.add_argument("--background", help="approved card whose conditions EVERY person must meet (e.g. the send "
                                        "conditions, for a content scenarios card)")
    p.add_argument("--db", help="team database: refuse cards that are not stored or are out of date (rules changed)")
    a = p.parse_args(argv)
    try:
        card = load_card(Path(a.card), preview=a.preview)
        for w in size_warnings(card):
            print(f"⚑ {w}")
        background = load_card(Path(a.background)) if a.background else None
        if a.db:
            _check_up_to_date(a.db, [c for c in (card, background) if c is not None])
        s = make_branches(card, force=a.force, edges=a.edges, background=background)
    except BranchError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if a.preview:
        print(f"*** PREVIEW of {card.id} v{card.version} ({card.status.value}): nothing written ***\n")
    print(preview(s, a.limit))
    if not a.preview:
        json_path, csv_path = write_branches(s, Path(a.out))
        print(f"\nWrote {json_path}\n  and {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
