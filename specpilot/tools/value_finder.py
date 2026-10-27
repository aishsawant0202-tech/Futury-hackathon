"""Value finder: turns each branch into a dummy person whose facts meet and fail exactly the right conditions.

For one branch ("C1 met, C2 met, C3 FAILED, C4 met, C5 met") it asks Z3 for a person, using the date model
(specpilot/tools/z3_model.py):
    every MET target       -> the condition must hold
    every FAILED target    -> the condition must not hold
    an edge target         -> the fact must equal the edge value exactly (e.g. total_days_12m = 29)
    free facts             -> left open here
It tries 0, 1, ... MAX_PREVIOUS earlier sick periods and takes the first that works.

Three outcomes, never mixed up:
    found       a person; immediately re-checked with the fact dictionary (a mismatch is a bug and stops the run)
    impossible  Z3 PROVED that no realistic person exists (with any number of earlier periods, for this
                current start date). Station 3 only compares conditions on the same fact; this catches clashes
                between facts, e.g. "earlier illness in the last 6 months" with "at most 1 sick day in total".
    unsolved    Z3 gave up (timeout). Not a proof of anything: reported as a problem, never as impossible.
Branches that Station 3 already marked impossible are passed through unchanged.
"""
import datetime as dt
from dataclasses import dataclass
from typing import Literal

import z3

from specpilot.branches import Branch, BranchSet, Status, Target
from specpilot.models import Scenario
from specpilot.tools import facts
from specpilot.tools.z3_model import MAX_PREVIOUS, condition, fact_exprs, person_vars, shape, to_scenario

TIMEOUT_MS = 10_000  # per solver call


class FinderBug(Exception):
    """A person Z3 found does not have the facts it was asked for: the date model and the fact dictionary
    disagree. This must never happen; the run stops."""


@dataclass
class Found:
    branch_id: str
    outcome: Literal["found", "impossible", "unsolved"]
    person: Scenario | None = None
    reason: str = ""


def constraints(branch: Branch, v) -> list[z3.BoolRef]:
    """What a person must satisfy to be this branch (its targets, and its background conditions, all met)."""
    exprs = fact_exprs(v)
    out = []
    for t in branch.targets + branch.background:
        cond = condition(exprs[t.fact], t.op, t.value)
        out.append(cond if t.target is Target.MET else z3.Not(cond))
        if t.at_edge:
            out.append(exprs[t.fact] == t.edge_value)
    return out


def check_person(branch: Branch, person: Scenario) -> list[str]:
    """Targets the person does NOT reach, recomputed with the fact dictionary (empty = all reached)."""
    values = facts.evaluate(person)
    wrong = []
    for t in branch.targets + branch.background:
        holds = facts.holds(values[t.fact], t.op, t.value)
        if holds != (t.target is Target.MET):
            wrong.append(f"{t.criterion_id} {t.describe()} should be {t.target.value} but {t.fact} = {values[t.fact]}")
        elif t.at_edge and values[t.fact] != t.edge_value:
            wrong.append(f"{t.criterion_id} should sit at the edge {t.edge_value} but {t.fact} = {values[t.fact]}")
    return wrong


def solve_branch(branch: Branch, start: dt.date, timeout_ms: int = TIMEOUT_MS,
                 extra: list | None = None) -> Found:
    """Find a person for one branch whose current sick period starts on `start`.
    `extra(v)` may add wishes (used for variety later); wishes are dropped again if they make it impossible."""
    if branch.status is Status.IMPOSSIBLE:
        return Found(branch.branch_id, "impossible", reason=f"(branch maker) {branch.reason}")
    gave_up = False
    for n in range(MAX_PREVIOUS + 1):
        ctx = z3.Context()  # fresh workspace per puzzle: same inputs, same person (see z3_model.person_vars)
        v = person_vars(start, n, ctx=ctx)
        s = z3.Solver(ctx=ctx)
        s.set("timeout", timeout_ms)
        s.add(shape(v) + constraints(branch, v))
        result = s.check()
        if result == z3.unknown:
            gave_up = True
            continue
        if result == z3.sat:
            person = to_scenario(v, s.model())
            wrong = check_person(branch, person)
            if wrong:
                raise FinderBug(f"{branch.branch_id}: the person found misses its targets: {'; '.join(wrong)}")
            return Found(branch.branch_id, "found", person)
    if gave_up:
        return Found(branch.branch_id, "unsolved",
                     reason=f"the solver gave up (timeout {timeout_ms} ms) for at least one number of earlier periods; "
                            f"this is NOT a proof that the branch is impossible")
    wanted = "; ".join(t.text() for t in branch.targets)
    return Found(branch.branch_id, "impossible",
                 reason=f"(value finder) proved: no realistic person with 0-{MAX_PREVIOUS} earlier sick periods and a "
                        f"current period starting {start:%d.%m.%Y} can do all of this: {wanted}")


def solve_all(branch_set: BranchSet, start: dt.date, timeout_ms: int = TIMEOUT_MS) -> list[Found]:
    """One result per branch, in the branch set's order."""
    return [solve_branch(b, start, timeout_ms) for b in branch_set.branches]


# --- variety: realistic, different people, reproducible from a seed -------------------------------------
#
# Without wishes Z3 returns extreme but valid people (1-day illnesses, dates on window edges). The dice
# now draw a realistic picture for every branch (start date in 2027, illness lengths, earlier illnesses,
# report date, free yes/no facts) and Z3 finds the person CLOSEST to that picture that still obeys every
# rule of the branch. The branch's conditions are hard; the wishes only steer the choice among valid people.
#
# Each person depends only on (seed, branch id): the same seed gives the same person for a branch, even if
# other branches are added or removed.

import random  # noqa: E402
from dataclasses import replace  # noqa: E402

YEAR = 2027
FIRST_START = dt.date(YEAR, 1, 1)
LAST_START = dt.date(YEAR, 11, 15)  # leaves room for the period and the report date inside 2027
LAST_DAY = dt.date(YEAR, 12, 31)
LADDER = (2, 7, 20, 60)  # how many days a person may be off each wish, tightest first; then: any valid person


@dataclass
class Wishes:
    start: dt.date
    n_previous: int  # preferred number of earlier illnesses (others are tried if this one cannot work)
    current_len: int
    report_offset: int
    previous: list[tuple[int, int]]  # (start, end) day numbers of the wished earlier illnesses, oldest first
    yes_no: dict[str, bool]  # wished values of the yes/no unknowns
    total_days: int  # wished total sick days: keeps people away from the limits (edge branches test those)


def make_rng(seed: int, branch_id: str) -> random.Random:
    return random.Random(f"{seed}:{branch_id}")  # string seeds are hashed the same way in every process


EDGE_DISTANCE = 3  # ordinary people stay this far from a limit; edge branches are the ones that sit on it


def limits(targets: list) -> list[int]:
    """The values where any of these targets switches between holding and not holding (30 and 29 for >= 30)."""
    lo, hi = facts.get(targets[0].fact).plausible
    return [x for x in range(lo - 1, hi + 2)
            if any(facts.holds(x, t.op, t.value) != facts.holds(x + d, t.op, t.value) for t in targets for d in (-1, 1))]


def wished_number(branch: Branch, fact: str, rng: random.Random) -> int | None:
    """A random value for a number fact that already lies on the side the branch needs, away from the limits.
    None if the branch says nothing about the fact."""
    targets = [t for t in branch.targets + branch.background if t.fact == fact]
    if not targets or facts.get(fact).type is not facts.FactType.INTEGER:
        return None
    lo, hi = facts.get(fact).plausible
    ok = [x for x in range(lo, hi + 1)
          if all(facts.holds(x, t.op, t.value) == (t.target is Target.MET) for t in targets)]
    if not ok:
        return None
    edges = limits(targets)
    calm = [x for x in ok if all(abs(x - edge) >= EDGE_DISTANCE for edge in edges) and x <= 120]
    return rng.choice(calm or ok)


def draw_wishes(rng: random.Random, branch: Branch | None = None) -> Wishes:
    start = FIRST_START + dt.timedelta(days=rng.randint(0, (LAST_START - FIRST_START).days))
    current_len = rng.randint(3, 21) if rng.random() < 0.8 else rng.randint(22, 45)
    n = rng.choice([0, 1, 1, 2, 2, 3, 4])
    previous, cursor = [], start.toordinal()
    for _ in range(n):
        end = cursor - rng.randint(10, 120)
        begin = end - rng.randint(1, 20)
        previous.insert(0, (begin, end))
        cursor = begin
    return Wishes(
        start=start, n_previous=n, current_len=current_len, report_offset=rng.randint(3, 30), previous=previous,
        yes_no={"current_certified": rng.random() < 0.9, "open_end": rng.random() < 0.25,
                "statutory": rng.random() < 0.85, "marginal": rng.random() < 0.1,
                **{f"prev{i}_certified": rng.random() < 0.85 for i in range(MAX_PREVIOUS)}},
        total_days=(wished_number(branch, "total_days_12m", rng) if branch else None)
        or rng.choice([rng.randint(5, 24), rng.randint(35, 90)]),
    )


def _near(x, target, tol):
    return z3.And(x >= target - tol, x <= target + tol)


def _wishes_within(v, w: Wishes, n: int, tol: int, branch: Branch) -> list[z3.BoolRef]:
    """Every date wish kept within `tol` days, and number facts kept away from the limits (ordinary people
    should not sit on a limit: that is what the edge branches are for)."""
    exprs = fact_exprs(v)
    s = v.s
    out = [_near(v.current_end, s + w.current_len - 1, tol), _near(v.report, s + w.report_offset, tol),
           _near(exprs["total_days_12m"], w.total_days, tol)]
    if n:  # the newest earlier illnesses follow the newest wished ones
        out += [c for p, (ws, we) in zip(v.previous[max(0, n - len(w.previous)):], w.previous[-n:])
                for c in (_near(p.start, ws, tol), _near(p.end, we, tol))]
    for t in branch.targets + branch.background:
        if facts.get(t.fact).type is facts.FactType.INTEGER and not t.at_edge:
            out += [z3.Or(exprs[t.fact] <= x - EDGE_DISTANCE, exprs[t.fact] >= x + EDGE_DISTANCE) for x in limits([t])]
    return out


def _flag_wishes(v, w: Wishes) -> list[z3.BoolRef]:
    flags = {"current_certified": v.current_certified, "open_end": v.open_end, "statutory": v.statutory,
             "marginal": v.marginal, **{f"prev{i}_certified": p.certified for i, p in enumerate(v.previous)}}
    return [var == w.yes_no[name] for name, var in flags.items()]


def _counts_to_try(preferred: int) -> list[int]:
    """The wished number of earlier illnesses first, then the nearest others."""
    return sorted(range(MAX_PREVIOUS + 1), key=lambda n: (abs(n - preferred), n))


OTHER_STARTS = 7  # further current start dates tried before a branch is called impossible


def _ladder(branch: Branch, w: "Wishes", timeout_ms: int) -> Found | None:
    """The wish ladder for one start date: a person near the wishes, or None."""
    ctx = z3.Context()  # one fresh workspace: the attempts always run in the same order (reproducible)
    for tol in LADDER:
        for n in _counts_to_try(w.n_previous):
            v = person_vars(w.start, n, ctx=ctx)
            s = z3.Solver(ctx=ctx)
            s.set("timeout", timeout_ms)
            year = [v.current_end <= LAST_DAY.toordinal(), v.report <= LAST_DAY.toordinal()]
            s.add(shape(v) + constraints(branch, v) + year + _wishes_within(v, w, n, tol, branch))
            for extra in (_flag_wishes(v, w), []):
                s.push()
                s.add(extra)
                if s.check() == z3.sat:
                    person = to_scenario(v, s.model())
                    wrong = check_person(branch, person)
                    if wrong:
                        raise FinderBug(f"{branch.branch_id}: the person found misses its targets: {'; '.join(wrong)}")
                    return Found(branch.branch_id, "found", person)
                s.pop()
    return None


def _try_start(branch: Branch, w: "Wishes", timeout_ms: int) -> Found:
    """found / impossible (for this start date) / unsolved, for one start date."""
    found = _ladder(branch, w, timeout_ms)
    if found:
        return found
    plain = solve_branch(branch, w.start, timeout_ms)  # any valid person, or a proof for this start date
    if plain.outcome == "found" and (plain.person.current.end > LAST_DAY or plain.person.report_date > LAST_DAY):
        return Found(branch.branch_id, "unsolved",
                     reason=f"only a person running past {LAST_DAY:%d.%m.%Y} exists for start {w.start:%d.%m.%Y}")
    return plain


def other_starts(seed: int, branch_id: str, first: dt.date) -> list[dt.date]:
    """Further start dates, reproducible from the seed: some random, plus a month start, middle and end."""
    rng = make_rng(seed, branch_id + ":other-starts")
    span = (LAST_START - FIRST_START).days
    dates = [FIRST_START + dt.timedelta(days=rng.randint(0, span)) for _ in range(OTHER_STARTS - 3)]
    dates += [dt.date(YEAR, 3, 1), dt.date(YEAR, 6, 15), dt.date(YEAR, 9, 30)]
    return [d for d in dict.fromkeys(dates) if d != first]


def solve_varied(branch: Branch, seed: int, timeout_ms: int = TIMEOUT_MS) -> Found:
    """A realistic person for the branch, steered by the seed's wishes.

    The branch's conditions (and background conditions) are always hard rules. The wishes are tried as a ladder
    of quick questions: "a valid person within 2 days of every wish, with the wished yes/no values?", then without
    the yes/no wishes, then within 7 days, 20, 60. The first yes wins. If no rung works, the plain solver finds
    any valid person or proves that none exists FOR THAT START DATE. Some edges exist only for some start dates
    (month-end clamping: e.g. no free time of exactly 6 months ends a day before 31.08.), so before calling a
    branch impossible, other start dates are tried, each with its own wishes.
    """
    if branch.status is Status.IMPOSSIBLE:
        return Found(branch.branch_id, "impossible", reason=f"(branch maker) {branch.reason}")
    w = draw_wishes(make_rng(seed, branch.branch_id), branch)
    tried = [w.start]
    result = _try_start(branch, w, timeout_ms)
    if result.outcome in ("found", "unsolved"):
        return result  # other start dates are only worth trying after a PROOF that this one cannot work
    gave_up = False
    for start in other_starts(seed, branch.branch_id, w.start):
        shift = start.toordinal() - w.start.toordinal()
        moved = replace(w, start=start, previous=[(a + shift, b + shift) for a, b in w.previous])
        tried.append(start)
        result = _try_start(branch, moved, timeout_ms)
        if result.outcome == "found":
            return result
        gave_up = gave_up or result.outcome == "unsolved"
    if gave_up:
        return Found(branch.branch_id, "unsolved",
                     reason=f"no person found for {len(tried)} start dates and the solver gave up on at least one; "
                            f"this is NOT a proof that the branch is impossible")
    wanted = "; ".join(t.text() for t in branch.targets + branch.background)
    return Found(branch.branch_id, "impossible",
                 reason=f"(value finder) proved for each of {len(tried)} start dates "
                        f"({', '.join(f'{d:%d.%m.%Y}' for d in tried)}): no realistic person with 0-{MAX_PREVIOUS} "
                        f"earlier sick periods can do all of this: {wanted}")


def find_people(branch_set: BranchSet, seed: int, timeout_ms: int = TIMEOUT_MS) -> list[Found]:
    """One realistic person (or a proof / problem) per branch, reproducible from the seed."""
    return [solve_varied(b, seed, timeout_ms) for b in branch_set.branches]


# --- the handover to Station 5, and the command line --------------------------------------------------------
#
# Station 4 never writes people to disk: its command only shows a preview. Station 5 calls make_people(),
# checks every person, and only then writes the SQL file, so unchecked dummy data never exists on disk.

import argparse  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator  # noqa: E402

VERSION = "value_finder 0.1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Person(_Model):
    person_id: str = Field(pattern=r"-P\d+$", description="branch id + -P1")
    branch_id: str
    scenario: Scenario


class NoPerson(_Model):
    branch_id: str
    reason: str = Field(min_length=5)


class PeopleSet(_Model):
    """Everything Station 4 hands to Station 5: one entry per branch, nothing missing, nothing twice."""

    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    seed: int
    made_by: str = VERSION
    branch_ids: list[str] = Field(min_length=1, description="Every branch of the branch set, in order")
    people: list[Person]
    impossible: list[NoPerson] = Field(default_factory=list)
    unsolved: list[NoPerson] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_branch_once(self):
        accounted = [p.branch_id for p in self.people] + [x.branch_id for x in self.impossible + self.unsolved]
        if sorted(accounted) != sorted(self.branch_ids) or len(set(accounted)) != len(accounted):
            missing = sorted(set(self.branch_ids) - set(accounted))
            extra = sorted({b for b in accounted if accounted.count(b) > 1 or b not in self.branch_ids})
            raise ValueError(f"every branch must appear exactly once; missing {missing}, twice or unknown {extra}")
        ids = [p.person_id for p in self.people]
        if len(set(ids)) != len(ids):
            raise ValueError("a person id appears twice")
        for p in self.people:
            if not p.person_id.startswith(p.branch_id + "-P"):
                raise ValueError(f"{p.person_id} does not belong to branch {p.branch_id}")
        return self

    def summary(self) -> str:
        return (f"{self.card_id} v{self.card_version}, seed {self.seed}: {len(self.branch_ids)} branches -> "
                f"{len(self.people)} people, {len(self.impossible)} impossible, {len(self.unsolved)} unsolved")


def make_people(branch_set: BranchSet, seed: int, timeout_ms: int = TIMEOUT_MS) -> PeopleSet:
    results = find_people(branch_set, seed, timeout_ms)
    return PeopleSet(
        card_id=branch_set.card_id, card_version=branch_set.card_version, card_hash=branch_set.card_hash, seed=seed,
        branch_ids=[b.branch_id for b in branch_set.branches],
        people=[Person(person_id=f"{r.branch_id}-P1", branch_id=r.branch_id, scenario=r.person)
                for r in results if r.outcome == "found"],
        impossible=[NoPerson(branch_id=r.branch_id, reason=r.reason) for r in results if r.outcome == "impossible"],
        unsolved=[NoPerson(branch_id=r.branch_id, reason=r.reason) for r in results if r.outcome == "unsolved"],
    )


class InputError(Exception):
    """The branches file cannot be used; the message says why."""


def load_branches(path: Path) -> BranchSet:
    try:
        return BranchSet.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except OSError as err:
        raise InputError(f"cannot read {path}: {err.strerror}") from None
    except ValidationError as err:
        raise InputError(f"{path} is not a valid branches file: "
                         f"{err.errors()[0]['msg'].removeprefix('Value error, ')}") from None


def preview(ps: PeopleSet, branch_set: BranchSet, limit: int = 40) -> str:
    patterns = {b.branch_id: b.pattern for b in branch_set.branches}
    lines = [ps.summary(), ""]
    lines.append(f"  {'person':<32} {'met?':<{max(4, branch_set.n_criteria)}}  current sick period     report  "
                 f"open  earlier  total  statutory  minijob")
    for p in ps.people[:limit]:
        s = p.scenario
        total = facts.evaluate(s)["total_days_12m"]
        lines.append(f"  {p.person_id:<32} {patterns[p.branch_id]:<{max(4, branch_set.n_criteria)}}  "
                     f"{s.current.start:%d.%m.%Y}-{s.current.end:%d.%m.%Y}  {s.report_date:%d.%m}   "
                     f"{'yes' if s.open_end else 'no':<4}  {len(s.previous):<7}  {total:<5}  "
                     f"{'yes' if s.statutory_insured else 'no':<9}  {'yes' if s.marginal_employment else 'no'}")
    if len(ps.people) > limit:
        lines.append(f"  ... {len(ps.people) - limit} more (--limit {len(ps.people)} shows all)")
    for title, items in (("Impossible", ps.impossible), ("UNSOLVED (problem, not a proof)", ps.unsolved)):
        if items:
            lines.append(f"\n  {title}:")
            lines += [f"  ✘ {x.branch_id}: {x.reason}" for x in items]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Preview the dummy people for a branches file (writes nothing; "
                                            "Station 5 checks them and writes the SQL file).")
    p.add_argument("branches", help="branches JSON from python -m specpilot.tools.branch_maker")
    p.add_argument("--seed", type=int, help="same seed = same people; a random one is chosen and shown if left out")
    p.add_argument("--limit", type=int, default=40, help="people to show on screen")
    p.add_argument("--timeout-ms", type=int, default=TIMEOUT_MS)
    a = p.parse_args(argv)
    seed = a.seed if a.seed is not None else secrets.randbelow(1_000_000)
    try:
        branch_set = load_branches(Path(a.branches))
    except InputError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"*** PREVIEW: nothing is written. Seed {seed}" + ("" if a.seed is not None else f" (repeat with --seed {seed})")
          + " ***\n")
    ps = make_people(branch_set, seed, a.timeout_ms)
    print(preview(ps, branch_set, a.limit))
    if ps.unsolved:
        print(f"\nerror: {len(ps.unsolved)} branch(es) unsolved; try a longer --timeout-ms or another seed",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
