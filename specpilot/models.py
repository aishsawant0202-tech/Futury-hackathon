"""Shared data shapes. Every agent and tool reads and writes these, and the database stores them.

The chain they form:
    spec page -> Requirement -> TestCase -> ExpectedResult -> Finding
and every step along the way leaves an AuditEntry.
"""
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    # extra="forbid": a model that invents a field gets a validation error instead of silently passing
    model_config = ConfigDict(extra="forbid")


class RuleStatus(str, Enum):
    DRAFT = "draft"  # found by the extraction agent, not yet reviewed
    APPROVED = "approved"  # examiner accepted it at gate 1; only these get test cases
    REJECTED = "rejected"


class RuleKind(str, Enum):
    PRECONDITION = "precondition"  # when a message may/must be sent at all
    CONTENT = "content"  # what a field must contain
    ORDER = "order"  # ordering of repeated entries
    FORMAT = "format"  # length, type, position in the record


class CaseKind(str, Enum):
    POSITIVE = "positive"  # rule is met
    NEGATIVE = "negative"  # rule is violated
    BOUNDARY = "boundary"  # exactly on a limit (30 days, 6 months)


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class Layer(str, Enum):
    FORMAT = "A"  # record can be parsed, lengths and types are right
    FIELDS = "B"  # field-by-field against the expected result
    RULES = "C"  # rule checks on the parsed record
    CAUSE = "D"  # model-proposed cause of a failure


class Requirement(_Model):
    """One rule from the spec, always tied to the page and the exact words it came from."""

    id: str = Field(pattern=r"^R-\d+(\.\d+)*-\d{3}$", description="e.g. R-3.12-001 (section + running number)")
    section: str = Field(description="Spec section, e.g. 3.12")
    page: int = Field(ge=1, description="Printed page number in the spec ('Seite x von y')")
    quote: str = Field(min_length=10, description="Exact German text from that page")
    statement: str = Field(description="The rule in one plain English sentence")
    kind: RuleKind
    document: str = "Verfahrensbeschreibung DTA EEL 13.4"
    # Set by code, not by the model
    quote_verified: bool = False
    votes: int = Field(default=1, ge=0, description="In how many of the extraction runs the rule appeared")
    flags: list[str] = Field(default_factory=list, description="Contradictions or doubts for the examiner")
    status: RuleStatus = RuleStatus.DRAFT
    reviewer_note: str = ""


class AbsencePeriod(_Model):
    """One sick-leave period of a test employee. Both dates count (inclusive)."""

    start: date
    end: date
    certified: bool = True  # False = sick note-free short absence (EFZG), not a potential previous illness

    @model_validator(mode="after")
    def _end_not_before_start(self):
        if self.end < self.start:
            raise ValueError(f"period ends {self.end} before it starts {self.start}")
        return self


class Scenario(_Model):
    """The test employee's situation that the payroll software is given."""

    report_date: date = Field(description="Day the employer sends the request")
    current: AbsencePeriod
    previous: list[AbsencePeriod] = Field(default_factory=list)
    statutory_insured: bool = True  # False = private health insurance
    marginal_employment: bool = False  # Minijob
    open_end: bool = False  # current absence has no end date yet in the payroll system


class TestCase(_Model):
    """A test employee designed to prove (or break) one or more approved rules."""

    __test__ = False  # stops pytest from treating this class as a test

    id: str = Field(pattern=r"^TC-\d+(\.\d+)*-\d{3}$", description="e.g. TC-3.12-001")
    requirement_ids: list[str] = Field(min_length=1)
    kind: CaseKind
    title: str
    check: str = Field(description="The YES/NO question the case answers")
    scenario: Scenario
    instructions: str = Field(default="", description="What the tester enters in the payroll software")


class ExpectedResult(_Model):
    """What correct software must output for a test case. Produced by the reference model, never by an LLM."""

    test_case_id: str
    message_expected: bool = Field(description="Must a reason 41 message be created at all?")
    fields: dict[str, str] = Field(default_factory=dict, description="Field name -> exact value as in the file")
    periods: list[AbsencePeriod] = Field(default_factory=list, description="Reported periods, in required order")
    produced_by: str = "reference_model"


class Finding(_Model):
    """One result of comparing actual output to the expected result."""

    test_case_id: str
    verdict: Verdict
    layer: Layer
    requirement_id: str | None = None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None
    message: str = ""
    cause: str = ""  # proposed by the model in layer D
    cause_confirmed: bool = False  # set by code after checking the cause on other test cases


class AuditEntry(_Model):
    """One row per step, so every decision can be traced: who, what, with which model, how long."""

    at: datetime = Field(default_factory=datetime.now)
    actor: str = Field(description="admin | extractor | tester | validator | examiner | code")
    action: str
    subject_id: str = ""  # the Requirement / TestCase id the step was about
    model: str = ""
    cache_hit: bool = False
    duration_s: float = 0.0
    detail: str = ""
