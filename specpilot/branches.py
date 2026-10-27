"""Branches: every met / not-met combination of an approved card's conditions.

A branch is a recipe for one dummy person: "meet C1, C2, C4; fail C3 and C5". The branch maker
writes them; the value finder (Station 4) turns each one into a person with real values.

For a card with n conditions there are 2^n branches per reading, numbered B01 ... in binary order
(C1 changes slowest): B01 = all met, the last = none met.

Rules enforced here, so an incomplete or inconsistent set can never be handed on:
- a branch set holds EXACTLY the 2^n distinct combinations for every reading: nothing missing, nothing twice
- every target is a valid condition from the fact dictionary
- all_true and kind always agree with the targets and the card's logic (AND / OR)
- an impossible branch says why; a possible one carries no reason
- an edge branch pins one number condition right at its limit (the last value that meets it, or the
  first that fails it) and names the branch it was derived from
- every branch names the card version (and its fingerprint) it was made from
"""
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from specpilot.criteria import Value, check_condition
from specpilot.tools import facts
from specpilot.tools.facts import FactType

BRANCH_ID = r"^REQ-[0-9A-Za-z][0-9A-Za-z.-]*?(-R\d+)?-B\d{2,}(-E\d+)?$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(str, Enum):
    MET = "met"
    FAILED = "failed"


class Kind(str, Enum):
    ALL_MET = "all_met"
    SINGLE_FAIL = "single_fail"
    MULTI_FAIL = "multi_fail"
    NONE_MET = "none_met"


class Status(str, Enum):
    POSSIBLE = "possible"
    IMPOSSIBLE = "impossible"


def kind_of(targets: list[Target]) -> Kind:
    failed = sum(t is Target.FAILED for t in targets)
    if failed == 0:
        return Kind.ALL_MET
    if failed == 1:
        return Kind.SINGLE_FAIL
    if failed == len(targets):
        return Kind.NONE_MET
    return Kind.MULTI_FAIL


def all_true_of(logic: str, targets: list[Target]) -> bool:
    met = [t is Target.MET for t in targets]
    return all(met) if logic == "AND" else any(met)


class CriterionTarget(_Model):
    """One condition of the card and whether this branch must meet or fail it."""

    criterion_id: str = Field(pattern=r"^C[1-9]\d*$")
    fact: str
    op: str
    value: Value
    target: Target
    at_edge: StrictBool = False  # edge branch: pinned right at the limit
    edge_value: StrictInt | None = None

    @model_validator(mode="after")
    def _valid(self):
        check_condition(self.fact, self.op, self.value)
        if self.at_edge:
            if facts.get(self.fact).type is not FactType.INTEGER:
                raise ValueError(f"{self.criterion_id}: only number conditions have an edge")
            if self.edge_value is None:
                raise ValueError(f"{self.criterion_id}: an edge target needs its edge_value")
            should_hold = self.target is Target.MET
            if facts.holds(self.edge_value, self.op, self.value) != should_hold:
                raise ValueError(f"{self.criterion_id}: edge value {self.edge_value} does not "
                                 f"{'meet' if should_hold else 'fail'} {self.describe()}")
            neighbours = (self.edge_value - 1, self.edge_value + 1)
            if not any(facts.holds(v, self.op, self.value) != should_hold for v in neighbours):
                raise ValueError(f"{self.criterion_id}: edge value {self.edge_value} is not at the limit of "
                                 f"{self.describe()} (one step either way gives the same result)")
        elif self.edge_value is not None:
            raise ValueError(f"{self.criterion_id}: edge_value is only for edge targets")
        return self

    def describe(self) -> str:
        v = f"[{self.value[0]}, {self.value[1]}]" if isinstance(self.value, list) else str(self.value).lower()
        return f"{self.fact} {self.op} {v}"

    def text(self) -> str:
        edge = f" exactly at the limit ({self.fact} = {self.edge_value})" if self.at_edge else ""
        return f"{self.criterion_id}: {self.describe()} -> must be {self.target.value.upper()}{edge}"


class Branch(_Model):
    branch_id: str = Field(pattern=BRANCH_ID)
    number: int = Field(ge=1, description="Position in binary order within its reading: 1 = all met")
    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    logic: Literal["AND", "OR"]
    reading: dict[str, str] = Field(default_factory=dict, description="C id -> reading label; empty = main readings")
    targets: list[CriterionTarget] = Field(min_length=1)
    all_true: StrictBool
    kind: Kind
    status: Status = Status.POSSIBLE
    reason: str = Field(default="", description="Why the branch is impossible")
    free_facts: list[str] = Field(default_factory=list, description="Facts the card does not constrain")
    edge_of: str = Field(default="", description="For an edge branch: the branch it was derived from")
    background: list[CriterionTarget] = Field(
        default_factory=list, description="Conditions EVERY person must meet, from another approved card (not branched)")

    @model_validator(mode="after")
    def _consistent(self):
        ids = [t.criterion_id for t in self.targets]
        if ids != [f"C{n}" for n in range(1, len(ids) + 1)]:
            raise ValueError(f"targets must be C1, C2, ... in order; got {ids}")
        wanted = [t.target for t in self.targets]
        if self.all_true != all_true_of(self.logic, wanted):
            raise ValueError(f"all_true={self.all_true} does not follow from {self.logic} of {self.pattern}")
        if self.kind is not kind_of(wanted):
            raise ValueError(f"kind {self.kind.value} does not match {self.pattern} (should be {kind_of(wanted).value})")
        if self.status is Status.IMPOSSIBLE and not self.reason.strip():
            raise ValueError(f"{self.branch_id}: an impossible branch must say why")
        if self.status is Status.POSSIBLE and self.reason:
            raise ValueError(f"{self.branch_id}: a possible branch has no reason")
        for f in self.free_facts:
            if f not in facts.FACTS:
                raise ValueError(f"unknown fact {f!r} in free_facts; allowed: {', '.join(facts.FACTS)}")
        clash = set(self.free_facts) & {t.fact for t in self.targets + self.background}
        if clash:
            raise ValueError(f"{sorted(clash)} cannot be free: the card or its background has a condition on it")
        bg_ids = [t.criterion_id for t in self.background]
        if bg_ids != [f"C{n}" for n in range(1, len(bg_ids) + 1)]:
            raise ValueError(f"background conditions must be C1, C2, ... of the background card; got {bg_ids}")
        if any(t.target is not Target.MET or t.at_edge for t in self.background):
            raise ValueError(f"{self.branch_id}: every background condition must be met, and none is an edge")
        edges = [t for t in self.targets if t.at_edge]
        if bool(edges) != bool(self.edge_of):
            raise ValueError(f"{self.branch_id}: edge targets and edge_of go together")
        if len(edges) > 1:
            raise ValueError(f"{self.branch_id}: an edge branch pins one condition, not {len(edges)}")
        return self

    @property
    def pattern(self) -> str:
        """✔✘✔... in C1..Cn order."""
        return "".join("✔" if t.target is Target.MET else "✘" for t in self.targets)

    @property
    def is_edge(self) -> bool:
        return bool(self.edge_of)


class BranchSet(_Model):
    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    logic: Literal["AND", "OR"]
    n_criteria: int = Field(ge=1)
    readings: list[dict[str, str]] = Field(min_length=1, description="Each reading combination branched; {} = main")
    branches: list[Branch]
    made_by: str = "branch_maker"
    background_card_id: str = Field(default="", description="Approved card whose conditions every person meets")
    background_card_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    @model_validator(mode="after")
    def _complete(self):
        ids = [b.branch_id for b in self.branches]
        if len(set(ids)) != len(ids):
            raise ValueError("a branch id appears twice")
        known = {json_key(r) for r in self.readings}
        if len(known) != len(self.readings):
            raise ValueError("a reading combination is listed twice")
        for b in self.branches:
            if (b.card_id, b.card_version, b.card_hash, b.logic) != (self.card_id, self.card_version, self.card_hash, self.logic):
                raise ValueError(f"{b.branch_id} comes from a different card version than the set")
            if len(b.targets) != self.n_criteria:
                raise ValueError(f"{b.branch_id} has {len(b.targets)} targets, the card has {self.n_criteria}")
            if json_key(b.reading) not in known:
                raise ValueError(f"{b.branch_id} uses a reading that is not in the set: {b.reading}")

        if bool(self.background_card_id) != bool(self.background_card_hash):
            raise ValueError("background card id and fingerprint go together")
        backgrounds = {json.dumps([t.model_dump(mode="json") for t in b.background]) for b in self.branches}
        if len(backgrounds) > 1:
            raise ValueError("all branches of a set must have the same background conditions")
        if bool(self.background_card_id) != any(b.background for b in self.branches[:1]):
            raise ValueError("background conditions need the background card's id and fingerprint, and vice versa")

        full = 2 ** self.n_criteria
        for r in self.readings:
            main = [b for b in self.branches if json_key(b.reading) == json_key(r) and not b.is_edge]
            patterns = [b.pattern for b in main]
            if len(main) != full or len(set(patterns)) != full:
                raise ValueError(f"reading {r or 'main'}: {len(set(patterns))} distinct combinations, "
                                 f"need all {full} (2^{self.n_criteria})")
            if sorted(b.number for b in main) != list(range(1, full + 1)):
                raise ValueError(f"reading {r or 'main'}: branches must be numbered 1..{full}")
            by_id = {b.branch_id for b in main}
            for e in (b for b in self.branches if b.is_edge and json_key(b.reading) == json_key(r)):
                if e.edge_of not in by_id:
                    raise ValueError(f"edge branch {e.branch_id} points to unknown branch {e.edge_of}")
        return self

    def summary(self) -> str:
        main = [b for b in self.branches if not b.is_edge]
        imp = sum(b.status is Status.IMPOSSIBLE for b in main)
        edges = len(self.branches) - len(main)
        bg = (f"; every person also meets {len(self.branches[0].background)} condition(s) of {self.background_card_id}"
              if self.background_card_id else "")
        return (f"{self.card_id} v{self.card_version}: {len(self.readings)} reading(s) x 2^{self.n_criteria} = "
                f"{len(main)} branches ({len(main) - imp} possible, {imp} impossible) + {edges} edge branch(es){bg}")


def json_key(reading: dict[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(reading.items()))
