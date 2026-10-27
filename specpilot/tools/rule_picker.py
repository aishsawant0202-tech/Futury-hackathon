"""Rule picker: choose the extracted rules a criteria card is built from. Plain code, no model.

Reads the extractor's `requirements` table and, for one card (e.g. REQ-41-PRECONDITION), keeps
the rules that are
    - about that message      (reason code 41 named on the rule)
    - the employer's duty     (the payroll software under test must do it; insurer duties are context)
    - of the card's kind      (precondition = when the message may be sent at all)
    - quote verified          (the German quote was found on its page, so the rule is not invented)
Every rule about that message that is NOT picked is listed with the reason(s), so nothing
disappears silently. Rules for other messages are only counted.

The pick gives the translator its input and fixes the card's source_rule_ids: the card must use
or explain exactly these rules (enforced by specpilot/criteria.py).

Run: python -m specpilot.tools.rule_picker [card id] [--db path] [--include-general]
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, field

from specpilot import db
from specpilot.models import REASON_CODES, Actor, Requirement, RuleKind


@dataclass(frozen=True)
class CardSpec:
    """Which rules belong on a card."""

    card_id: str
    title: str
    reason_code: str
    kind: RuleKind
    actor: Actor = Actor.EMPLOYER
    sections: tuple[str, ...] = ()  # empty = every section

    def __post_init__(self):
        if self.reason_code not in REASON_CODES:
            raise ValueError(f"unknown reason code {self.reason_code!r}")


CARDS: dict[str, CardSpec] = {s.card_id: s for s in (
    CardSpec(
        card_id="REQ-41-PRECONDITION",
        title="When may the employer send reason 41 (Anforderung Vorerkrankungsmitteilungen)?",
        reason_code="41",
        kind=RuleKind.PRECONDITION,
    ),
)}


class PickError(Exception):
    """No card can be built: unknown card, empty database, or no rule passes the filters."""


@dataclass
class Exclusion:
    rule: Requirement
    reasons: list[str]


@dataclass
class Pick:
    spec: CardSpec
    picked: list[Requirement]
    excluded: list[Exclusion] = field(default_factory=list)
    other_messages: int = 0  # rules for other reason codes, not considered

    @property
    def rule_ids(self) -> list[str]:
        return [r.id for r in self.picked]

    def warnings(self) -> list[str]:
        """Picked rules the reviewer should look at closely."""
        out = []
        for r in self.picked:
            if r.votes <= 1:
                out.append(f"{r.id}: found in only {r.votes} extraction reading(s), check it is a real rule")
            out += [f"{r.id}: {f}" for f in r.flags]
        return out

    def rules_hash(self) -> str:
        """Fingerprint of the picked rules. If the extractor changes one of them, this changes,
        so a card built on the old rules can be detected and reviewed again."""
        content = [r.model_dump(mode="json", include={"id", "page", "quote", "statement", "kind", "actor",
                                                      "reason_codes", "document"}) for r in self.picked]
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def skeleton(self) -> dict:
        """The card fields fixed by the pick. The translator adds criteria / unused_rules / missing_facts."""
        return {"id": self.spec.card_id, "title": self.spec.title, "reason_codes": [self.spec.reason_code],
                "kind": self.spec.kind.value, "source_rule_ids": self.rule_ids}

    def describe(self) -> str:
        s = self.spec
        lines = [f"{s.card_id}: {len(self.picked)} rule(s) picked "
                 f"(Abgabegrund {s.reason_code}, {s.actor.value}, {s.kind.value}, quote verified)"]
        for r in self.picked:
            lines.append(f"  ✔ {r.id:<14} p.{r.page:<4} votes={r.votes}  {r.statement}")
        if self.excluded:
            lines.append(f"\n  {len(self.excluded)} rule(s) about Abgabegrund {s.reason_code} not picked:")
            for e in self.excluded:
                lines.append(f"  ✘ {e.rule.id:<14} p.{e.rule.page:<4} {'; '.join(e.reasons)}")
        warnings = self.warnings()
        if warnings:
            lines.append("\n  Check:")
            lines += [f"  ⚑ {w}" for w in warnings]
        lines.append(f"\n  {self.other_messages} rule(s) for other messages not considered.")
        return "\n".join(lines)


def _reasons_to_exclude(r: Requirement, spec: CardSpec, include_general: bool) -> list[str]:
    reasons = []
    if not r.reason_codes and not include_general:
        reasons.append("applies to every message, not specific to this one (use --include-general to pick it)")
    if r.actor != spec.actor:
        reasons.append(f"duty of the {r.actor.value}, not the {spec.actor.value}")
    if r.kind != spec.kind:
        reasons.append(f"a {r.kind.value} rule, not a {spec.kind.value}")
    if not r.quote_verified:
        reasons.append("quote not verified on its page")
    if spec.sections and r.section not in spec.sections:
        reasons.append(f"in § {r.section}, outside {', '.join(spec.sections)}")
    return reasons


def pick(conn: sqlite3.Connection, card_id: str, include_general: bool = False) -> Pick:
    try:
        spec = CARDS[card_id]
    except KeyError:
        raise PickError(f"unknown card {card_id!r}; known: {', '.join(CARDS)}") from None

    every = db.list_requirements(conn)
    if not every:
        raise PickError("the database has no extracted rules; run the extractor first "
                        "(python -m specpilot.agents.extractor)")
    about = [r for r in every if not r.reason_codes or spec.reason_code in r.reason_codes]
    order = lambda r: (r.page, r.id)  # noqa: E731 - page order, as in the spec

    picked, excluded = [], []
    for r in sorted(about, key=order):
        reasons = _reasons_to_exclude(r, spec, include_general)
        if reasons:
            excluded.append(Exclusion(r, reasons))
        else:
            picked.append(r)
    if not picked:
        raise PickError(f"no rule passes the filters for {card_id}; "
                        f"{len(excluded)} rule(s) about Abgabegrund {spec.reason_code} were all excluded")
    return Pick(spec, picked, excluded, other_messages=len(every) - len(about))


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Show which extracted rules a criteria card is built from.")
    p.add_argument("card", nargs="?", default="REQ-41-PRECONDITION", choices=list(CARDS))
    p.add_argument("--db", help="database file (default: specpilot.db)")
    p.add_argument("--include-general", action="store_true", help="also pick rules that apply to every message")
    a = p.parse_args(argv)
    try:
        result = pick(db.connect(a.db), a.card, a.include_general)
    except PickError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(result.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
