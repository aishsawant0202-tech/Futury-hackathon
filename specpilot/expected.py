"""Expected results: what correct payroll software must produce for each dummy person (Station 6, level 1).

Level 1 answers one question per person: must the message (e.g. reason 41) be sent at all? The answer comes
from the approved card's conditions, recomputed from the person's RAW values, and is explained condition
by condition with the rule and German quote it rests on.

These are the ANSWERS. They are written to their own file, never into the dummy file that goes to the
software developers.

Rules enforced here:
- every condition result is internally consistent: its met/failed follows from the recomputed value
- message_expected follows from the card's AND / OR logic over the conditions
- an expected file has exactly one result per dummy person and reading: nobody missing, nobody twice
- it names the card version and the exact dummy file (by fingerprint) it was computed from
"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from specpilot.criteria import Value, check_condition
from specpilot.tools import facts
from specpilot.tools.facts import FactType

VERSION = "expected 0.1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def reading_key(reading: dict[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(reading.items())) or "main"


class ConditionResult(_Model):
    criterion_id: str = Field(pattern=r"^C[1-9]\d*$")
    fact: str
    op: str
    limit: Value = Field(description="The card's value, e.g. 30 in total_days_12m >= 30")
    value: StrictBool | StrictInt = Field(description="The person's value, recomputed from the raw data")
    met: StrictBool
    rule_ids: list[str] = Field(min_length=1)
    quote: str = Field(min_length=3)

    @model_validator(mode="after")
    def _consistent(self):
        check_condition(self.fact, self.op, self.limit)
        boolean = facts.get(self.fact).type is FactType.BOOLEAN
        if boolean != (type(self.value) is bool):
            raise ValueError(f"{self.criterion_id}: {self.fact} is {'yes/no' if boolean else 'a number'}, "
                             f"not {self.value!r}")
        if self.met != facts.holds(self.value, self.op, self.limit):
            raise ValueError(f"{self.criterion_id}: met={self.met} does not follow from "
                             f"{self.fact} = {self.value} {self.op} {self.limit}")
        return self

    def condition(self) -> str:
        limit = f"[{self.limit[0]}, {self.limit[1]}]" if isinstance(self.limit, list) else json.dumps(self.limit)
        return f"{self.fact} {self.op} {limit}"

    def text(self) -> str:
        shown = json.dumps(self.value) if isinstance(self.value, bool) else str(self.value)
        return (f"{self.criterion_id} {'✔' if self.met else '✘'} {self.condition()} (person: {shown}) "
                f"- {', '.join(self.rule_ids)}: „{self.quote}“")


class ExpectedResult(_Model):
    person_id: str = Field(pattern=r"-P\d+$")
    neutral_id: str = Field(pattern=r"^DUMMY-\d{4,}$")
    reading: dict[str, str] = Field(default_factory=dict, description="{} = the card's main readings")
    logic: Literal["AND", "OR"]
    conditions: list[ConditionResult] = Field(min_length=1)
    message_expected: StrictBool

    @model_validator(mode="after")
    def _consistent(self):
        ids = [c.criterion_id for c in self.conditions]
        if ids != [f"C{n}" for n in range(1, len(ids) + 1)]:
            raise ValueError(f"{self.person_id}: conditions must be C1, C2, ... in order; got {ids}")
        met = [c.met for c in self.conditions]
        if self.message_expected != (all(met) if self.logic == "AND" else any(met)):
            raise ValueError(f"{self.person_id}: message_expected={self.message_expected} does not follow from "
                             f"{self.logic} of the conditions")
        return self

    def explanation(self) -> str:
        head = (f"{self.neutral_id} ({self.person_id}), reading {reading_key(self.reading)}: message "
                f"{'MUST be sent' if self.message_expected else 'must NOT be sent'}")
        return "\n".join([head] + [f"  {c.text()}" for c in self.conditions])


class ExpectedFile(_Model):
    card_id: str
    card_version: int = Field(ge=1)
    card_hash: str = Field(min_length=64, max_length=64)
    seed: int
    dummy_file: str = Field(description="Name of the dummy SQL file the results were computed from")
    dummy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Fingerprint of that exact file")
    logic: Literal["AND", "OR"]
    n_criteria: int = Field(ge=1)
    made_by: str = VERSION
    person_ids: list[str] = Field(min_length=1, description="Every person in the dummy file")
    readings: list[dict[str, str]] = Field(min_length=1)
    results: list[ExpectedResult]

    @model_validator(mode="after")
    def _complete(self):
        if len(set(self.person_ids)) != len(self.person_ids):
            raise ValueError("a person appears twice in person_ids")
        keys = [reading_key(r) for r in self.readings]
        if len(set(keys)) != len(keys):
            raise ValueError("a reading is listed twice")
        wanted = {(p, k) for p in self.person_ids for k in keys}
        got = [(r.person_id, reading_key(r.reading)) for r in self.results]
        if len(set(got)) != len(got):
            twice = sorted({g for g in got if got.count(g) > 1})
            raise ValueError(f"more than one result for {twice[:3]}")
        if set(got) != wanted:
            missing, extra = sorted(wanted - set(got)), sorted(set(got) - wanted)
            raise ValueError(f"one result per person and reading: missing {missing[:3]}, unexpected {extra[:3]}")
        for r in self.results:
            if r.logic != self.logic or len(r.conditions) != self.n_criteria:
                raise ValueError(f"{r.person_id}: logic or number of conditions differs from the card")
        neutral: dict[str, str] = {}
        for r in self.results:
            if neutral.setdefault(r.person_id, r.neutral_id) != r.neutral_id:
                raise ValueError(f"{r.person_id} has two different neutral ids")
        return self

    def summary(self) -> str:
        send = sum(r.message_expected for r in self.results)
        return (f"{self.card_id} v{self.card_version}, seed {self.seed}: {len(self.person_ids)} people x "
                f"{len(self.readings)} reading(s) = {len(self.results)} expected results; message must be sent in "
                f"{send}, must not be sent in {len(self.results) - send}")
