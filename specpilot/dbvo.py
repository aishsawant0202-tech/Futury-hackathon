"""DBVO content inventory (Station 6 level 2, step 1): which fields the reason 41 request must contain and
which content rules decide them, per reading of the spec.

The inventory is data (data/content/REQ-41-CONTENT.inventory.json), checked by the form below:
    fields  name, meaning, format (jjjjmmtt = date YYYYMMDD, nn = two-digit number), whether it repeats per
            reported period (BEGINN-AU-n / ENDE-AU-n), and the spec source
    rules   the content rules as named parameters with one value per reading, e.g. lookback_months:
            p.135 -> no limit, p.136 -> 12. The calculator (later step) implements the fixed logic; the values
            come from here, each with its source.

Every source is marked as a placeholder until checked against the spec PDF: the content is taken from the
team's notes, not from the PDF itself. Nothing here uses the team's reference model.
"""
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from specpilot import config
from specpilot.models import REASON_CODES

INVENTORY_DIR = config.ROOT / "data" / "content"

# The content rules the calculator knows how to apply, and the values each may take
RULE_VALUES: dict[str, type | tuple] = {
    "only_with_message": bool,
    "certified_only": bool,
    "gap_months": int,
    "gap_exactly_counts": bool,
    "lookback_months": (int, type(None)),
    "order": ("newest_first", "oldest_first"),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Model):
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=5)
    placeholder: bool = True


class FieldSpec(_Model):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+)*(-n)?$")
    meaning: str = Field(min_length=5)
    format: Literal["jjjjmmtt", "nn"]
    repeats: bool
    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self):
        if self.repeats != self.name.endswith("-n"):
            raise ValueError(f"{self.name}: a field that repeats per period ends in -n, and only such a field")
        return self


class ContentRule(_Model):
    key: str
    meaning: str = Field(min_length=5)
    values: dict[str, bool | int | str | None]
    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self):
        if self.key not in RULE_VALUES:
            raise ValueError(f"unknown content rule {self.key!r}; known: {', '.join(RULE_VALUES)}")
        allowed = RULE_VALUES[self.key]
        for reading, v in self.values.items():
            if isinstance(allowed, tuple) and all(isinstance(a, str) for a in allowed):
                ok = v in allowed
            elif allowed is int:
                ok = type(v) is int and v > 0
            elif allowed is bool:
                ok = type(v) is bool
            else:  # int or None
                ok = v is None or (type(v) is int and v > 0)
            if not ok:
                raise ValueError(f"{self.key}: {v!r} is not allowed for reading {reading}")
        return self


class Inventory(_Model):
    card_id: str = Field(pattern=r"^REQ-[0-9A-Za-z][0-9A-Za-z.-]*$")
    title: str = Field(min_length=5)
    reason_code: str
    readings: list[str] = Field(min_length=1)
    fields: list[FieldSpec] = Field(min_length=1)
    rules: list[ContentRule] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self):
        if self.reason_code not in REASON_CODES:
            raise ValueError(f"unknown reason code {self.reason_code!r}")
        if len(set(self.readings)) != len(self.readings):
            raise ValueError("a reading is listed twice")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("a field is listed twice")
        repeating = {n.removesuffix("-n") for n in names if n.endswith("-n")}
        if repeating and repeating != {"BEGINN-AU", "ENDE-AU"}:
            raise ValueError(f"repeating fields must be the BEGINN-AU-n / ENDE-AU-n pair, got {sorted(repeating)}")
        if repeating and "ANZAHL-AU" not in names:
            raise ValueError("repeating period fields need ANZAHL-AU (how many there are)")
        keys = [r.key for r in self.rules]
        if len(set(keys)) != len(keys):
            raise ValueError("a content rule is listed twice")
        missing = set(RULE_VALUES) - set(keys)
        if missing:
            raise ValueError(f"content rule(s) missing: {sorted(missing)}")
        for r in self.rules:
            if set(r.values) != set(self.readings):
                raise ValueError(f"{r.key}: needs exactly one value per reading {self.readings}, has {sorted(r.values)}")
        return self

    def rule(self, key: str, reading: str):
        """The value of a content rule under one reading."""
        if reading not in self.readings:
            raise KeyError(f"unknown reading {reading!r}; readings: {self.readings}")
        return next(r for r in self.rules if r.key == key).values[reading]

    def disagreements(self) -> list[str]:
        """Rules whose value differs between readings: where the spec contradicts itself."""
        return [r.key for r in self.rules if len({json.dumps(v) for v in r.values.values()}) > 1]

    def placeholders(self) -> int:
        return sum(s.placeholder for x in [*self.fields, *self.rules] for s in x.sources)

    def describe(self) -> str:
        lines = [f"{self.card_id}  {self.title}  (Abgabegrund {self.reason_code}, readings: {', '.join(self.readings)})",
                 "", "  Fields:"]
        for f in self.fields:
            lines.append(f"    {f.name:<13} {f.format:<9} {'per period' if f.repeats else 'once':<10} {f.meaning}")
        lines += ["", "  Content rules:" + " " * 24 + "  ".join(f"{r:<14}" for r in self.readings)]
        for r in self.rules:
            values = "  ".join(f"{json.dumps(r.values[x]):<14}" for x in self.readings)
            flag = "  ⚑ readings differ" if r.key in self.disagreements() else ""
            lines.append(f"    {r.key:<36}{values}{flag}")
        lines.append(f"\n  {self.placeholders()} source(s) still PLACEHOLDER: confirm against the spec PDF.")
        lines += [f"  ? {q}" for q in self.open_questions]
        return "\n".join(lines)


def load(card_id: str = "REQ-41-CONTENT", folder: Path = INVENTORY_DIR) -> Inventory:
    path = Path(folder) / f"{card_id}.inventory.json"
    return Inventory.model_validate_json(path.read_text(encoding="utf-8"))




# --- the content card: the inventory as an approvable card ----------------------------------------------------
#
# Only an APPROVED content card is used by the DBVO calculator. Approval is locked to the content (like a
# criteria card): any change afterwards voids it. While sources are placeholders, approval needs an explicit
# acceptance, and the number of accepted placeholders is recorded on the card.

import hashlib  # noqa: E402
from datetime import datetime  # noqa: E402

from specpilot.criteria import CardStatus  # noqa: E402

CONTENT_FIELDS = ("card_id", "title", "reason_code", "readings", "fields", "rules", "version")


class ContentCard(Inventory):
    version: int = Field(default=1, ge=1)
    status: CardStatus = CardStatus.DRAFT
    reviewer: str = ""
    reviewed_at: datetime | None = None
    review_note: str = ""
    approved_hash: str = ""
    approved_with_placeholders: int = Field(default=0, ge=0)
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _approval(self):
        if self.status is CardStatus.APPROVED:
            if not self.reviewer or self.reviewed_at is None:
                raise ValueError("an approved content card needs the reviewer and the time of approval")
            if self.approved_hash != self.content_hash():
                raise ValueError("the content card changed after it was approved: review it again")
            if self.approved_with_placeholders != self.placeholders():
                raise ValueError("the number of accepted placeholder sources does not match the card")
        elif self.status is CardStatus.REJECTED:
            if not self.reviewer or self.reviewed_at is None or not self.review_note.strip():
                raise ValueError("a rejected content card needs the reviewer, the time and a reason")
        elif self.approved_hash or self.approved_with_placeholders:
            raise ValueError("a draft content card cannot carry an approval")
        return self

    def content_hash(self) -> str:
        content = self.model_dump(mode="json", include=set(CONTENT_FIELDS))
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @property
    def approval_valid(self) -> bool:
        return self.status is CardStatus.APPROVED and bool(self.reviewer) and self.approved_hash == self.content_hash()


def content_card(inv: Inventory) -> ContentCard:
    """A fresh draft content card from the inventory."""
    return ContentCard.model_validate(inv.model_dump())


def load_card(path: Path) -> ContentCard:
    return ContentCard.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_card(card: ContentCard, path: Path) -> None:
    Path(path).write_text(card.model_dump_json(indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Show the DBVO content inventory, or save it as a draft content card.")
    p.add_argument("--card-out", help="save a draft content card here, for python -m specpilot.review")
    a = p.parse_args(argv)
    inv = load()
    print(inv.describe())
    if a.card_out:
        save_card(content_card(inv), Path(a.card_out))
        print(f"\nDraft content card saved to {a.card_out}. Review it: "
              f"python -m specpilot.review {a.card_out} --reviewer \"Name\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- content results: what the DBVO block must contain, per person and reading ------------------------------------

import datetime as dt  # noqa: E402

FIELD_DATE = r"^\d{8}$"


class ReportedPeriod(_Model):
    n: int = Field(ge=1, description="1 = the first reported period (the newest, with order newest_first)")
    start: dt.date
    end: dt.date
    why: str = Field(min_length=5, description="Why this period is in the chain")

    @model_validator(mode="after")
    def _valid(self):
        if self.end < self.start:
            raise ValueError(f"reported period {self.n} ends before it starts")
        return self


class ContentResult(_Model):
    person_id: str = Field(pattern=r"-P\d+$")
    neutral_id: str = Field(pattern=r"^DUMMY-\d{4,}$")
    reading: str
    message_expected: bool
    fields: dict[str, str] = Field(default_factory=dict, description="Field name -> exact value, e.g. ANZAHL-AU -> 02")
    periods: list[ReportedPeriod] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list, description="Earlier absences left out, with the reason")
    stop_reason: str = ""

    @model_validator(mode="after")
    def _consistent(self):
        if not self.message_expected:
            if self.fields or self.periods:
                raise ValueError(f"{self.person_id}: no message, so no content")
            return self
        if [p.n for p in self.periods] != list(range(1, len(self.periods) + 1)):
            raise ValueError(f"{self.person_id}: reported periods must be numbered 1, 2, ...")
        if self.fields.get("ANZAHL-AU") != f"{len(self.periods):02d}":
            raise ValueError(f"{self.person_id}: ANZAHL-AU {self.fields.get('ANZAHL-AU')!r} does not match "
                             f"{len(self.periods)} reported period(s)")
        for p in self.periods:
            if (self.fields.get(f"BEGINN-AU-{p.n}"), self.fields.get(f"ENDE-AU-{p.n}")) != (
                    f"{p.start:%Y%m%d}", f"{p.end:%Y%m%d}"):
                raise ValueError(f"{self.person_id}: BEGINN-AU-{p.n} / ENDE-AU-{p.n} do not match period {p.n}")
        extra = [k for k in self.fields if k.startswith(("BEGINN-AU-", "ENDE-AU-"))
                 and int(k.rsplit("-", 1)[1]) > len(self.periods)]
        if extra:
            raise ValueError(f"{self.person_id}: fields {extra} beyond the {len(self.periods)} reported period(s)")
        for name, value in self.fields.items():
            if (name.startswith(("BEGINN", "ENDE")) or name == "AU-AB-AG") and not re.match(FIELD_DATE, value):
                raise ValueError(f"{self.person_id}: {name} must be a date YYYYMMDD, not {value!r}")
        return self

    def explanation(self) -> str:
        head = f"{self.neutral_id} ({self.person_id}), reading {self.reading}: "
        if not self.message_expected:
            return head + "no message, so no DBVO content"
        lines = [head + f"DBVO with {len(self.periods)} earlier period(s)"]
        lines += [f"  {k} = {v}" for k, v in self.fields.items()]
        lines += [f"  period {p.n}: {p.start:%d.%m.%Y}-{p.end:%d.%m.%Y} - {p.why}" for p in self.periods]
        lines += [f"  skipped: {s}" for s in self.skipped]
        lines.append(f"  stopped: {self.stop_reason}")
        return "\n".join(lines)


class ContentFile(_Model):
    """All DBVO content results for one dummy file: one per person and reading, nobody missing or twice."""

    content_card_id: str
    content_card_version: int = Field(ge=1)
    content_card_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_with_placeholders: int = Field(ge=0)
    precondition_card_id: str
    precondition_card_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dummy_file: str
    dummy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int
    readings: list[str] = Field(min_length=1)
    person_ids: list[str] = Field(min_length=1)
    results: list[ContentResult]
    made_by: str = "dbvo_calc 0.1"

    @model_validator(mode="after")
    def _complete(self):
        wanted = {(p, r) for p in self.person_ids for r in self.readings}
        got = [(x.person_id, x.reading) for x in self.results]
        if len(set(got)) != len(got):
            raise ValueError("more than one content result for a person and reading")
        if set(got) != wanted:
            raise ValueError(f"one content result per person and reading: missing {sorted(wanted - set(got))[:3]}, "
                             f"unexpected {sorted(set(got) - wanted)[:3]}")
        return self

    def summary(self) -> str:
        send = sum(r.message_expected for r in self.results)
        differ = sum(1 for p in self.person_ids
                     if len({json.dumps(r.fields, sort_keys=True) for r in self.results if r.person_id == p}) > 1)
        return (f"{len(self.person_ids)} people x {len(self.readings)} readings = {len(self.results)} DBVO results; "
                f"{send} with a message; content differs between the readings for {differ} people")
