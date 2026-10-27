"""Criteria cards: one requirement written as sub-criteria the generator can work with.

The translator (rule sentences -> card) fills these in; a person approves them; the dummy data
generator reads only approved cards. A card turns the extractor's sentences

    R-3.12-004  "...zusammen mit der aktuellen AU-Zeit mindestens 30 Tage umfassen."

into machine-readable parts

    C3  total_days_12m  >=  30     (from R-3.12-004, quote "mindestens 30 Tage")

Rules enforced here, so a broken card can never be stored or used:
- every sub-criterion names a fact from the fact dictionary (specpilot/tools/facts.py),
  with an operator and a value that fit that fact's type
- every sub-criterion cites the extracted rule(s) and the German words it comes from
- every picked rule is used by a sub-criterion or explained (unused, or a fact is missing)
- where the spec allows two readings, both are kept as alternatives, never silently one
- an approval is tied to the exact content: change the card and the approval no longer counts
"""
import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from specpilot import config
from specpilot.models import ReasonCode, RuleKind, Scenario
from specpilot.tools import facts
from specpilot.tools.facts import FactType

RULE_ID = r"^R-\d+(\.\d+)*-\d{3}$"  # same pattern as models.Requirement.id

Value = StrictBool | StrictInt | list[StrictInt]  # strict: "30" or "true" as text are rejected


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_condition(fact: str, op: str, value) -> None:
    """Raise ValueError unless `fact op value` is a valid condition for the fact dictionary."""
    try:
        f = facts.get(fact)
    except KeyError as err:
        raise ValueError(err.args[0]) from None
    if op not in f.ops:
        raise ValueError(f"operator {op!r} is not allowed for {fact} ({f.type.value}); allowed: {' '.join(f.ops)}")
    if f.type is FactType.BOOLEAN:
        if type(value) is not bool:
            raise ValueError(f"{fact} is yes/no, so the value must be true or false, not {value!r}")
    elif op == "between":
        if not (isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value)):
            raise ValueError(f"between needs [low, high] as two whole numbers, not {value!r}")
        if value[0] > value[1]:
            raise ValueError(f"between: low {value[0]} is above high {value[1]}")
    elif type(value) is not int:
        raise ValueError(f"{fact} is a number, so the value must be a whole number, not {value!r}")


def _check_rule_id(rid: str) -> None:
    if not re.fullmatch(RULE_ID, rid):
        raise ValueError(f"{rid!r} is not a rule id like R-3.12-004")


class Condition(_Model):
    """fact op value, with the evidence it comes from."""

    fact: str = Field(description="A name from the fact dictionary")
    op: str
    value: Value
    source_rule_ids: list[str] = Field(min_length=1, description="Extracted rule(s) this condition comes from")
    quote: str = Field(min_length=3, description="The German words in those rules that state this condition")

    @model_validator(mode="after")
    def _valid(self):
        check_condition(self.fact, self.op, self.value)
        for rid in self.source_rule_ids:
            _check_rule_id(rid)
        return self

    def describe(self) -> str:
        value = f"[{self.value[0]}, {self.value[1]}]" if isinstance(self.value, list) else json.dumps(self.value)
        return f"{self.fact} {self.op} {value}"

    def holds(self, values: dict[str, bool | int]) -> bool:
        return facts.holds(values[self.fact], self.op, self.value)


class Alternative(Condition):
    """Another reading of the same condition, when the spec can be read two ways."""

    label: str = Field(min_length=2, description="Which reading, e.g. 'p.136: strictly less than 6 months'")


class SubCriterion(Condition):
    id: str = Field(pattern=r"^C[1-9]\d*$", description="C1, C2, ... in order")
    label: str = Field(default="", description="Name of the main reading when there are alternatives")
    ambiguity: str = Field(default="", description="Why the spec allows more than one reading")
    alternatives: list[Alternative] = Field(default_factory=list)

    @model_validator(mode="after")
    def _alternatives(self):
        if self.alternatives and not self.ambiguity.strip():
            raise ValueError(f"{self.id}: alternatives need an 'ambiguity' explaining why the spec allows them")
        if self.alternatives and not self.label.strip():
            raise ValueError(f"{self.id}: name the main reading in 'label' when there are alternatives")
        seen = {(self.op, json.dumps(self.value))}
        labels = {self.label}
        for alt in self.alternatives:
            if alt.fact != self.fact:
                raise ValueError(f"{self.id}: an alternative must be about the same fact ({self.fact}), not {alt.fact}")
            key = (alt.op, json.dumps(alt.value))
            if key in seen:
                raise ValueError(f"{self.id}: alternative '{alt.label}' is identical to another reading")
            if alt.label in labels:
                raise ValueError(f"{self.id}: two readings are both labelled '{alt.label}'")
            seen.add(key)
            labels.add(alt.label)
        return self

    def reading(self, label: str | None) -> Condition:
        """The main condition (label None or the main label) or the alternative with that label."""
        if label is None or label == self.label:
            return self
        for alt in self.alternatives:
            if alt.label == label:
                return alt
        raise KeyError(f"{self.id} has no reading {label!r}; readings: {[self.label] + [a.label for a in self.alternatives]}")


class UnusedRule(_Model):
    """A picked rule that does not become a sub-criterion, with the reason."""

    rule_id: str
    reason: str = Field(min_length=5)

    @model_validator(mode="after")
    def _valid(self):
        _check_rule_id(self.rule_id)
        return self


class MissingFact(_Model):
    """A condition the translator could not express because the fact dictionary lacks a fact for it."""

    rule_ids: list[str] = Field(min_length=1)
    quote: str = Field(min_length=3)
    needed: str = Field(min_length=5, description="What the missing fact would have to measure")

    @model_validator(mode="after")
    def _valid(self):
        for rid in self.rule_ids:
            _check_rule_id(rid)
        return self


class CardStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


# Fields that make up the card's content. Reviewing fields (status, reviewer, ...) are not part of it.
CONTENT_FIELDS = ("id", "title", "reason_codes", "kind", "logic", "criteria", "source_rule_ids",
                  "unused_rules", "missing_facts", "document", "version")


class CriteriaCard(_Model):
    id: str = Field(pattern=r"^REQ-[0-9A-Za-z][0-9A-Za-z.-]*$", description="e.g. REQ-41-PRECONDITION")
    title: str = Field(min_length=5)
    reason_codes: list[ReasonCode] = Field(min_length=1)
    kind: RuleKind
    logic: Literal["AND", "OR"] = "AND"
    criteria: list[SubCriterion] = Field(min_length=1)
    source_rule_ids: list[str] = Field(min_length=1, description="Every extracted rule picked for this card")
    unused_rules: list[UnusedRule] = Field(default_factory=list)
    missing_facts: list[MissingFact] = Field(default_factory=list)
    document: str = config.SPEC_DOCUMENT
    # Set by code and by the reviewer, never by the model
    version: int = Field(default=1, ge=1)
    produced_by: str = Field(default="", description="Model id, or 'hand'")
    flags: list[str] = Field(default_factory=list, description="Warnings from the checks, for the reviewer")
    status: CardStatus = CardStatus.DRAFT
    reviewer: str = ""
    reviewed_at: datetime | None = None
    review_note: str = ""
    approved_hash: str = Field(default="", description="content_hash() at the moment of approval")

    @model_validator(mode="after")
    def _consistent(self):
        ids = [c.id for c in self.criteria]
        if ids != [f"C{n}" for n in range(1, len(ids) + 1)]:
            raise ValueError(f"criteria must be numbered C1, C2, ... in order; got {ids}")

        for rid in self.source_rule_ids:
            _check_rule_id(rid)
        if len(set(self.source_rule_ids)) != len(self.source_rule_ids):
            raise ValueError("source_rule_ids lists a rule twice")
        picked = set(self.source_rule_ids)

        used = {rid for c in self.criteria for cond in (c, *c.alternatives) for rid in cond.source_rule_ids}
        missing = {rid for m in self.missing_facts for rid in m.rule_ids}
        unused = [u.rule_id for u in self.unused_rules]
        for rid in sorted(used | missing | set(unused)):
            if rid not in picked:
                raise ValueError(f"{rid} is cited but is not in source_rule_ids")
        if len(set(unused)) != len(unused):
            raise ValueError("unused_rules lists a rule twice")
        both = used & set(unused)
        if both:
            raise ValueError(f"{sorted(both)} cannot be both used and unused")
        silent = [rid for rid in self.source_rule_ids if rid not in used | missing | set(unused)]
        if silent:
            raise ValueError(f"{silent} not accounted for: cite each picked rule in a criterion, "
                             f"or list it in unused_rules (with the reason) or missing_facts")

        if self.status is CardStatus.APPROVED:
            if not self.reviewer or self.reviewed_at is None:
                raise ValueError("an approved card needs the reviewer and the time of approval")
            if self.approved_hash != self.content_hash():
                raise ValueError("the card changed after it was approved: approval no longer valid, review again")
        elif self.status is CardStatus.REJECTED:
            if not self.reviewer or self.reviewed_at is None or not self.review_note.strip():
                raise ValueError("a rejected card needs the reviewer, the time and a reason")
        elif self.approved_hash:
            raise ValueError("a draft card cannot carry an approval")
        return self

    # --- identity ---

    def content_hash(self) -> str:
        """Fingerprint of the content. Any change to a criterion, value or source changes it."""
        content = self.model_dump(mode="json", include=set(CONTENT_FIELDS))
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @property
    def approval_valid(self) -> bool:
        """True only if approved AND unchanged since. (model_copy skips validation, so check here.)"""
        return self.status is CardStatus.APPROVED and bool(self.reviewer) and self.approved_hash == self.content_hash()

    # --- using the card ---

    def readings(self) -> dict[str, list[str]]:
        """Sub-criteria that have more than one reading: {C id: [main label, alternative labels...]}."""
        return {c.id: [c.label] + [a.label for a in c.alternatives] for c in self.criteria if c.alternatives}

    def conditions(self, choice: dict[str, str] | None = None) -> dict[str, Condition]:
        """{C id: condition}, using the main reading unless `choice` picks an alternative by label."""
        choice = choice or {}
        unknown = set(choice) - {c.id for c in self.criteria}
        if unknown:
            raise KeyError(f"no sub-criteria {sorted(unknown)} on {self.id}")
        return {c.id: c.reading(choice.get(c.id)) for c in self.criteria}

    def evaluate(self, s: Scenario, choice: dict[str, str] | None = None) -> dict[str, bool]:
        """Which sub-criteria a test person meets: {C1: True, C2: False, ...}."""
        values = facts.evaluate(s)
        return {cid: cond.holds(values) for cid, cond in self.conditions(choice).items()}

    def satisfied(self, s: Scenario, choice: dict[str, str] | None = None) -> bool:
        met = self.evaluate(s, choice).values()
        return all(met) if self.logic == "AND" else any(met)

    def describe(self) -> str:
        lines = [f"{self.id}  {self.title}   [{self.logic}, Abgabegrund {', '.join(self.reason_codes)}, "
                 f"{self.status.value}, v{self.version}]"]
        for c in self.criteria:
            lines.append(f"  {c.id}  {c.describe():<32} <- {', '.join(c.source_rule_ids)}: „{c.quote}“")
            for a in c.alternatives:
                lines.append(f"      or {a.describe():<29} ({a.label}) <- {', '.join(a.source_rule_ids)}: „{a.quote}“")
            if c.ambiguity:
                lines.append(f"      ⚑ ambiguous: {c.ambiguity}")
        for u in self.unused_rules:
            lines.append(f"  unused {u.rule_id}: {u.reason}")
        for m in self.missing_facts:
            lines.append(f"  ⚑ missing fact ({', '.join(m.rule_ids)}): {m.needed}")
        for f in self.flags:
            lines.append(f"  ⚑ {f}")
        return "\n".join(lines)
