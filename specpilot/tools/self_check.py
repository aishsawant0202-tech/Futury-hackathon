"""Self-check: proves the dummy data is right before anything is written. Plain Python, no Z3.

It trusts nothing Station 4 says. Every person is rebuilt from the RAW values in the dummy file only, its
facts are recomputed with the fact dictionary, and the result is compared with the approved card and the
branches. It checks ONLY the testing side's own material (card, branches, dummy file); it never computes
expected results or judges any software.

    card       approved, unchanged since approval, no placeholder rule ids
    branches   made from exactly this card version; every branch's conditions are the card's conditions
    coverage   every branch exactly once; no unsolved branch; no person for a branch the maker ruled out
    people     the truth labels are the branch's design; recomputed from the raw dates, every condition is met
               or failed exactly as designed; edge people hit their exact value; all_true follows
    realism    current period and report date in 2027; report 0-40 days after the start; every period
               1-60 days; earlier periods before the current one, with at least one free day between,
               within 500 days; no two people identical

Branches the value finder PROVED impossible cannot be re-proved without a solver: they are listed as notes,
never hidden. Any problem means the dummy file must not be written.
"""
from dataclasses import dataclass, field

from specpilot.branches import BranchSet, Status, Target, all_true_of
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.dummies import DummyFile, DummyPerson
from specpilot.tools import facts, gold
from specpilot.tools.z3_model import LOOKBACK_DAYS, MAX_LEN, REPORT_WITHIN

YEAR = 2027


@dataclass(frozen=True)
class Problem:
    where: str  # card, branches, a branch id or a person id
    check: str
    message: str

    def text(self) -> str:
        return f"[{self.check}] {self.where}: {self.message}"


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    people_checked: int = 0
    branches: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        head = (f"SELF-CHECK {'PASSED' if self.ok else 'FAILED'}: {self.people_checked} people checked against "
                f"{self.branches} branches, {len(self.problems)} problem(s)")
        lines = [head] + [f"  ✘ {p.text()}" for p in self.problems] + [f"  ⚑ {n}" for n in self.notes]
        return "\n".join(lines)


# --- the separate checks ------------------------------------------------------------------------------------

def check_card(card: CriteriaCard) -> list[Problem]:
    out = []
    if card.status is not CardStatus.APPROVED:
        out.append(Problem("card", "card", f"{card.id} v{card.version} is {card.status.value}, not approved"))
    elif not card.approval_valid:
        out.append(Problem("card", "card", f"{card.id}: changed after approval, the approval no longer counts"))
    ph = gold.placeholder_ids(card)
    if ph:
        out.append(Problem("card", "card", f"placeholder rule ids {ph}"))
    return out


def check_branches(card: CriteriaCard, bs: BranchSet) -> list[Problem]:
    if (bs.card_id, bs.card_version, bs.card_hash) != (card.id, card.version, card.content_hash()):
        return [Problem("branches", "branches", f"made from {bs.card_id} v{bs.card_version}, not from the approved "
                                                f"{card.id} v{card.version} (fingerprints differ)")]
    out = []
    for b in bs.branches:
        try:
            conditions = card.conditions(b.reading)
        except KeyError as err:
            out.append(Problem(b.branch_id, "branches", f"reading not on the card: {err.args[0]}"))
            continue
        for t in b.targets:
            c = conditions.get(t.criterion_id)
            if c is None or (c.fact, c.op, c.value) != (t.fact, t.op, t.value):
                out.append(Problem(b.branch_id, "branches", f"{t.criterion_id} says {t.describe()}, the card says "
                                                            f"{c.describe() if c else 'nothing'}"))
    return out


def check_background(bs: BranchSet, background: CriteriaCard | None) -> list[Problem]:
    """The background card must be given, approved, unchanged, and be the one the branches were made with."""
    if not bs.background_card_id:
        return [Problem("background", "background", "a background card was given, but the branches have none")]             if background is not None else []
    if background is None:
        return [Problem("background", "background", f"the branches were made with background card "
                                                    f"{bs.background_card_id}; it is needed for the check")]
    out = [Problem("background card", "background", p.message) for p in check_card(background)]
    if (background.id, background.content_hash()) != (bs.background_card_id, bs.background_card_hash):
        return out + [Problem("background card", "background", f"{background.id} is not the background card the "
                                                               f"branches were made with (fingerprints differ)")]
    wanted = [(c.id, c.fact, c.op, c.value) for c in background.criteria]
    for b in bs.branches:
        if [(t.criterion_id, t.fact, t.op, t.value) for t in b.background] != wanted:
            out.append(Problem(b.branch_id, "background", "background conditions differ from the background card"))
    return out


def check_coverage(bs: BranchSet, d: DummyFile) -> tuple[list[Problem], list[str]]:
    out, notes = [], []
    if (d.card_id, d.card_version, d.card_hash) != (bs.card_id, bs.card_version, bs.card_hash):
        out.append(Problem("dummy file", "coverage", "made from another card version than the branches"))
    covered = [c.branch_id for c in d.coverage]
    wanted = [b.branch_id for b in bs.branches]
    if covered != wanted:
        missing, unknown = sorted(set(wanted) - set(covered)), sorted(set(covered) - set(wanted))
        out.append(Problem("dummy file", "coverage", f"branches missing {missing}, unknown {unknown}"
                           if missing or unknown else "branches are out of order"))
    by_id = {b.branch_id: b for b in bs.branches}
    for c in d.coverage:
        b = by_id.get(c.branch_id)
        if c.outcome == "unsolved":
            out.append(Problem(c.branch_id, "coverage", f"no person and no proof (unsolved): {c.reason}"))
        elif c.outcome == "person" and b is not None and b.status is Status.IMPOSSIBLE:
            out.append(Problem(c.branch_id, "coverage", "a person for a branch the branch maker ruled impossible"))
        elif c.outcome == "impossible" and b is not None and b.status is not Status.IMPOSSIBLE:
            notes.append(f"{c.branch_id}: proven impossible by the value finder's solver (not re-checked here): "
                         f"{c.reason}")
    return out, notes


def check_person(card: CriteriaCard, bs: BranchSet, d: DummyFile, p: DummyPerson) -> list[Problem]:
    b = next((x for x in bs.branches if x.branch_id == p.branch_id), None)
    if b is None:
        return [Problem(p.person_id, "person", f"branch {p.branch_id} is not in the branches file")]
    out = []
    designed = {t.criterion_id: int(t.target is Target.MET) for t in b.targets}
    if p.truth != designed:
        out.append(Problem(p.person_id, "labels", f"truth labels {p.truth} are not the branch design {designed}"))

    values = facts.evaluate(d.scenario_of(p))
    recomputed = []
    for cid, cond in card.conditions(b.reading).items():
        holds = facts.holds(values[cond.fact], cond.op, cond.value)
        recomputed.append(Target.MET if holds else Target.FAILED)
        if int(holds) != designed[cid]:
            out.append(Problem(p.person_id, "conditions",
                               f"{cid} {cond.describe()} should be {'met' if designed[cid] else 'failed'}, but from the "
                               f"raw values {cond.fact} = {values[cond.fact]}"))
    for t in b.background:
        if not facts.holds(values[t.fact], t.op, t.value):
            out.append(Problem(p.person_id, "background",
                               f"background {t.criterion_id} {t.describe()} must be met, but from the raw values "
                               f"{t.fact} = {values[t.fact]}"))
    if p.all_true != int(all_true_of(card.logic, recomputed)):
        out.append(Problem(p.person_id, "conditions", f"all_true {p.all_true} does not match the recomputed conditions"))

    edge = next((t for t in b.targets if t.at_edge), None)
    if (edge.criterion_id if edge else "", edge.edge_value if edge else None) != (p.edge_criterion, p.edge_value):
        out.append(Problem(p.person_id, "edge", "edge labels differ from the branch"))
    if edge and values[edge.fact] != edge.edge_value:
        out.append(Problem(p.person_id, "edge", f"{edge.criterion_id} must sit exactly at {edge.edge_value}, "
                                                f"but {edge.fact} = {values[edge.fact]}"))
    return out + check_realism(d, p)


def check_realism(d: DummyFile, p: DummyPerson) -> list[Problem]:
    out = []

    def bad(message: str) -> None:
        out.append(Problem(p.person_id, "realism", message))

    for label, day in (("current start", p.current_start), ("current end", p.current_end), ("report date", p.report_date)):
        if day.year != YEAR:
            bad(f"{label} {day:%d.%m.%Y} is not in {YEAR}")
    offset = (p.report_date - p.current_start).days
    if not 0 <= offset <= REPORT_WITHIN:
        bad(f"report date is {offset} days after the current start (allowed 0-{REPORT_WITHIN})")
    if not 1 <= (p.current_end - p.current_start).days + 1 <= MAX_LEN:
        bad(f"current period lasts {(p.current_end - p.current_start).days + 1} days (allowed 1-{MAX_LEN})")
    earlier = sorted((q for q in d.previous if q.person_id == p.person_id), key=lambda q: q.number)
    for i, q in enumerate(earlier):
        length = (q.end - q.start).days + 1
        if not 1 <= length <= MAX_LEN:
            bad(f"earlier period {q.number} lasts {length} days (allowed 1-{MAX_LEN})")
        nxt = earlier[i + 1].start if i + 1 < len(earlier) else p.current_start
        if (nxt - q.end).days < 2:
            bad(f"earlier period {q.number} ends {q.end:%d.%m.%Y}, without a free day before {nxt:%d.%m.%Y}")
        if (p.current_start - q.start).days > LOOKBACK_DAYS:
            bad(f"earlier period {q.number} starts more than {LOOKBACK_DAYS} days before the current one")
    return out


def _raw(d: DummyFile, p: DummyPerson) -> str:
    return d.scenario_of(p).model_dump_json()


# --- all together ----------------------------------------------------------------------------------------------

def check(card: CriteriaCard, bs: BranchSet, d: DummyFile, background: CriteriaCard | None = None) -> Report:
    report = Report(branches=len(bs.branches))
    report.problems += check_card(card)
    report.problems += check_background(bs, background)
    if (d.background_card_id, d.background_card_hash) != (bs.background_card_id, bs.background_card_hash):
        report.problems.append(Problem("dummy file", "background", "the dummy file names another background card "
                                                                   "than the branches"))
    branch_problems = check_branches(card, bs)
    report.problems += branch_problems
    if branch_problems and branch_problems[0].where == "branches":
        return report  # wrong card version: comparing people against it would only add noise
    problems, notes = check_coverage(bs, d)
    report.problems += problems
    report.notes += notes
    seen: dict[str, str] = {}
    for p in d.people:
        report.problems += check_person(card, bs, d, p)
        raw = _raw(d, p)
        if raw in seen:
            report.problems.append(Problem(p.person_id, "duplicate", f"same raw values as {seen[raw]}"))
        seen.setdefault(raw, p.person_id)
        report.people_checked += 1
    return report
