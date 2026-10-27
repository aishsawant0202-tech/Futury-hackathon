"""The dummy file: what is written to SQL at the end of the testing pipeline.

Three tables, each row a strict form:
    dummy_people            one row per dummy person: raw values (what the payroll software is given),
                            traceability (card, branch, seed) and hidden labels (_truth per condition, all_true)
    dummy_previous_periods  the earlier sick periods of each person (0-4 each)
    dummy_coverage          one row per branch: a person, proven impossible, or unsolved, so every
                            scenario of the card is accounted for

Hidden labels say what a person was DESIGNED to be (from their branch): 1 = meant to meet the condition,
0 = meant to fail it. They are never sent to the software; the stripped CSV leaves them out and uses
neutral ids (DUMMY-0001, ...) because branch ids encode the pattern (B05 = "only C3 fails"). The neutral ids
are SHUFFLED with the seed: in branch order, DUMMY-0001 would always be the all-met person.
"""
import datetime as dt
import random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from specpilot.branches import BranchSet, Target, all_true_of
from specpilot.models import AbsencePeriod, Scenario

PROVENANCE = "SYNTHETIC"
MAX_PREVIOUS = 4


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DummyPerson(_Model):
    # identity and traceability
    person_id: str = Field(pattern=r"-P\d+$")
    neutral_id: str = Field(pattern=r"^DUMMY-\d{4,}$")
    branch_id: str
    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    seed: int
    provenance: Literal["SYNTHETIC"] = PROVENANCE
    made_by: str
    # raw values: what the payroll software is given
    report_date: dt.date
    current_start: dt.date
    current_end: dt.date
    current_certified: StrictBool
    open_end: StrictBool
    statutory_insured: StrictBool
    marginal_employment: StrictBool
    # hidden labels: what this person was designed to be
    truth: dict[str, StrictInt] = Field(description="C1..Cn -> 1 designed to meet, 0 designed to fail")
    all_true: StrictInt
    edge_criterion: str = ""
    edge_value: StrictInt | None = None

    @model_validator(mode="after")
    def _valid(self):
        if self.current_end < self.current_start:
            raise ValueError(f"{self.person_id}: current period ends before it starts")
        keys = list(self.truth)
        if keys != [f"C{n}" for n in range(1, len(keys) + 1)] or not keys:
            raise ValueError(f"{self.person_id}: truth must be C1, C2, ... in order; got {keys}")
        if any(v not in (0, 1) for v in self.truth.values()) or self.all_true not in (0, 1):
            raise ValueError(f"{self.person_id}: truth labels and all_true are 0 or 1")
        if bool(self.edge_criterion) != (self.edge_value is not None):
            raise ValueError(f"{self.person_id}: edge_criterion and edge_value go together")
        if self.edge_criterion and self.edge_criterion not in self.truth:
            raise ValueError(f"{self.person_id}: edge on unknown condition {self.edge_criterion}")
        if not self.person_id.startswith(self.branch_id + "-P"):
            raise ValueError(f"{self.person_id} does not belong to branch {self.branch_id}")
        return self


class PreviousPeriod(_Model):
    person_id: str
    number: int = Field(ge=1, le=MAX_PREVIOUS, description="1 = oldest")
    start: dt.date
    end: dt.date
    certified: StrictBool

    @model_validator(mode="after")
    def _valid(self):
        if self.end < self.start:
            raise ValueError(f"{self.person_id} period {self.number}: ends before it starts")
        return self


class Coverage(_Model):
    branch_id: str
    outcome: Literal["person", "impossible", "unsolved"]
    person_id: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _valid(self):
        if (self.outcome == "person") != bool(self.person_id):
            raise ValueError(f"{self.branch_id}: a person id exactly when the outcome is 'person'")
        if (self.outcome == "person") == bool(self.reason):
            raise ValueError(f"{self.branch_id}: a reason exactly when there is no person")
        return self


class DummyFile(_Model):
    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    seed: int
    logic: Literal["AND", "OR"]
    n_criteria: int = Field(ge=1)
    made_by: str
    people: list[DummyPerson]
    previous: list[PreviousPeriod]
    coverage: list[Coverage] = Field(min_length=1)
    background_card_id: str = ""
    background_card_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    @model_validator(mode="after")
    def _consistent(self):
        branches = [c.branch_id for c in self.coverage]
        if len(set(branches)) != len(branches):
            raise ValueError("a branch appears twice in the coverage")
        covered = {c.person_id: c.branch_id for c in self.coverage if c.outcome == "person"}
        ids = [p.person_id for p in self.people]
        if len(set(ids)) != len(ids):
            raise ValueError("a person id appears twice")
        if set(ids) != set(covered):
            raise ValueError(f"people and coverage disagree: only in people {sorted(set(ids) - set(covered))}, "
                             f"only in coverage {sorted(set(covered) - set(ids))}")
        neutral = [p.neutral_id for p in self.people]
        if sorted(neutral) != [f"DUMMY-{n:04d}" for n in range(1, len(neutral) + 1)]:
            raise ValueError("neutral ids must be exactly DUMMY-0001 ... DUMMY-<number of people>, each once")
        for p in self.people:
            if covered[p.person_id] != p.branch_id:
                raise ValueError(f"{p.person_id}: coverage puts it on another branch")
            if (p.card_id, p.card_version, p.card_hash, p.seed) != (self.card_id, self.card_version, self.card_hash, self.seed):
                raise ValueError(f"{p.person_id} comes from another card version or seed than the file")
            if len(p.truth) != self.n_criteria:
                raise ValueError(f"{p.person_id} has {len(p.truth)} truth labels, the card has {self.n_criteria}")
            designed = [Target.MET if v else Target.FAILED for v in p.truth.values()]
            if p.all_true != int(all_true_of(self.logic, designed)):
                raise ValueError(f"{p.person_id}: all_true does not follow from {self.logic} of its truth labels")
        numbers: dict[str, list[int]] = {}
        for q in self.previous:
            if q.person_id not in covered:
                raise ValueError(f"earlier period for unknown person {q.person_id}")
            numbers.setdefault(q.person_id, []).append(q.number)
        for pid, ns in numbers.items():
            if ns != list(range(1, len(ns) + 1)):
                raise ValueError(f"{pid}: earlier periods must be numbered 1, 2, ... (oldest first)")
        return self

    def scenario_of(self, person: DummyPerson) -> Scenario:
        """Rebuild the person as the team's Scenario, from the raw values only."""
        prev = sorted((q for q in self.previous if q.person_id == person.person_id), key=lambda q: q.number)
        return Scenario(report_date=person.report_date,
                        current=AbsencePeriod(start=person.current_start, end=person.current_end,
                                              certified=person.current_certified),
                        previous=[AbsencePeriod(start=q.start, end=q.end, certified=q.certified) for q in prev],
                        statutory_insured=person.statutory_insured, marginal_employment=person.marginal_employment,
                        open_end=person.open_end)


def neutral_numbers(count: int, seed: int) -> list[int]:
    """1..count in a seeded random order: neutral id numbers that say nothing about the branch."""
    numbers = list(range(1, count + 1))
    random.Random(f"{seed}:neutral-ids").shuffle(numbers)
    return numbers


def build(people_set, branch_set: BranchSet, made_by: str) -> DummyFile:
    """Station 4's people set -> the dummy file. Truth labels come from each person's branch (the design)."""
    if (people_set.card_id, people_set.card_version, people_set.card_hash) != (
            branch_set.card_id, branch_set.card_version, branch_set.card_hash):
        raise ValueError("the people were made from another card version than the branches")
    by_branch = {b.branch_id: b for b in branch_set.branches}
    numbers = neutral_numbers(len(people_set.people), people_set.seed)
    people, previous = [], []
    for n, p in zip(numbers, people_set.people):
        b = by_branch[p.branch_id]
        edge = next((t for t in b.targets if t.at_edge), None)
        s = p.scenario
        people.append(DummyPerson(
            person_id=p.person_id, neutral_id=f"DUMMY-{n:04d}", branch_id=b.branch_id, card_id=b.card_id,
            card_version=b.card_version, card_hash=b.card_hash, seed=people_set.seed, made_by=made_by,
            report_date=s.report_date, current_start=s.current.start, current_end=s.current.end,
            current_certified=s.current.certified, open_end=s.open_end, statutory_insured=s.statutory_insured,
            marginal_employment=s.marginal_employment,
            truth={t.criterion_id: int(t.target is Target.MET) for t in b.targets}, all_true=int(b.all_true),
            edge_criterion=edge.criterion_id if edge else "", edge_value=edge.edge_value if edge else None))
        previous += [PreviousPeriod(person_id=p.person_id, number=i, start=q.start, end=q.end, certified=q.certified)
                     for i, q in enumerate(s.previous, start=1)]
    person_of = {p.branch_id: p.person_id for p in people_set.people}
    coverage = []
    for bid in people_set.branch_ids:
        if bid in person_of:
            coverage.append(Coverage(branch_id=bid, outcome="person", person_id=person_of[bid]))
        else:
            other = next(x for x in people_set.impossible + people_set.unsolved if x.branch_id == bid)
            outcome = "impossible" if other in people_set.impossible else "unsolved"
            coverage.append(Coverage(branch_id=bid, outcome=outcome, reason=other.reason))
    return DummyFile(card_id=branch_set.card_id, card_version=branch_set.card_version, card_hash=branch_set.card_hash,
                     seed=people_set.seed, logic=branch_set.logic, n_criteria=branch_set.n_criteria, made_by=made_by,
                     people=people, previous=previous, coverage=coverage,
                     background_card_id=branch_set.background_card_id,
                     background_card_hash=branch_set.background_card_hash)
