"""Translator agent: turns the picked rule sentences into a criteria card (fact + operator + value).

Run: python -m specpilot.agents.translator [card id] [--db path] [--model name]

Flow:
  1. the rule picker chooses the rules (plain code, specpilot/tools/rule_picker.py)
  2. GLM gets those rules, the fact dictionary and an invented worked example, and fills in a CardDraft
     (fact names and operators are fixed lists in the schema, so it cannot invent new ones)
  3. code numbers the criteria C1, C2, ... and builds the CriteriaCard. The card's checks refuse a card
     that cites a rule outside the pick, leaves a picked rule unaccounted for, or has a wrong type.
     A refused card goes back to the model with the reason, up to `attempts` times.
  4. cross-check: the same rules are sent again in reverse order. If the second card means something
     different, the difference is flagged for the reviewer. (Temperature 0 + cache would make an
     identical second request pointless, so the order is what changes.)

The model only proposes. The result is always a DRAFT card; a person approves it.
The worked example in the prompt is invented, never taken from the gold card (the answer key).
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from specpilot import config, db, llm
from specpilot.criteria import Value, CriteriaCard, MissingFact, UnusedRule, check_condition
from specpilot.models import AuditEntry, Requirement
from specpilot.tools import card_guards, facts, gold, rule_picker
from specpilot.tools.card_guards import GERMAN_OPERATORS
from specpilot.tools.facts import OPS

log = logging.getLogger("specpilot.translator")

FactName = Literal[tuple(facts.FACTS)]  # type: ignore[valid-type]
Op = Literal[tuple(dict.fromkeys(op for ops in OPS.values() for op in ops))]  # type: ignore[valid-type]


# --- what the model fills in --------------------------------------------------------------

class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConditionDraft(_Draft):
    fact: FactName
    op: Op
    value: Value
    source_rule_ids: list[str] = Field(min_length=1, description="The rule id(s) this condition comes from")
    quote: str = Field(min_length=3, description="The German words from that rule's quote that state this condition")

    @model_validator(mode="after")
    def _fits_the_fact(self):
        check_condition(self.fact, self.op, self.value)  # type errors go straight back to the model
        return self


class AlternativeDraft(ConditionDraft):
    label: str = Field(description="Name of this reading")


class CriterionDraft(ConditionDraft):
    label: str = Field(default="", description="Name of the main reading, only when there are alternatives")
    ambiguity: str = Field(default="", description="Why the rules allow two readings, only when there are alternatives")
    alternatives: list[AlternativeDraft] = Field(default_factory=list)


class CardDraft(_Draft):
    logic: Literal["AND", "OR"]
    criteria: list[CriterionDraft] = Field(min_length=1)
    unused_rules: list[UnusedRule] = Field(default_factory=list)
    missing_facts: list[MissingFact] = Field(default_factory=list)


# --- the prompt ------------------------------------------------------------------------------

SYSTEM = """You turn rules extracted from a German specification for payroll software into a criteria card:
the list of conditions that must hold before the employer's payroll software may send one message.

Write every condition as fact + operator + value, using ONLY the facts listed below.
- One criterion per separate condition. A rule naming two conditions ("nicht für privat Versicherte oder
  Minijobs") becomes two criteria that both cite that rule.
- source_rule_ids: the id(s) of the rule(s) the condition comes from.
- quote: copy the German words of that rule that state the condition, e.g. "mindestens 30 Tage".
- value: true/false for yes/no facts, a whole number for number facts, [low, high] for "between".
- Turn German limit words into operators exactly:
{operators}
  A negative rule ("ist nicht möglich für privat Versicherte") becomes the positive condition
  (statutory_insured = true).
- Every rule listed must appear in exactly one place:
  * cited by a criterion, or
  * in unused_rules with a short reason (background, a definition of how a fact is counted, about what
    is reported rather than whether the message may be sent at all, a duty of someone else), or
  * in missing_facts, if it states a condition none of the facts can express. Never stretch a fact to fit.
- alternatives: only when the rules themselves give two different limits for the same condition.
  Then put the main reading in the criterion, the other in alternatives, label both, and explain in
  ambiguity. Never invent a reading.
- logic: "AND" if every condition must hold.
Do not use knowledge from outside the given rules.

FACTS
{facts}"""

# Invented, not from the spec and not the reason 41 answer. Shows a two-condition rule, a missing fact
# and an unused rule.
EXAMPLE_INPUT = """Card: EXAMPLE-99  "When may the invented message 99 be sent?" (invented example, not from the spec)
R-9.9-001 (p.1, votes 3): „Die Meldung ist nur zulässig, wenn eine Arbeitsunfähigkeitsbescheinigung vorliegt und die AU-Zeiten mehr als 42 Tage umfassen.“ - Only with a sick note and more than 42 days.
R-9.9-002 (p.1, votes 3): „Sie gilt nur für Beschäftigte, die das 25. Lebensjahr vollendet haben.“ - Only for employees aged 25 or older.
R-9.9-003 (p.1, votes 2): „Das Verfahren dient der Entlastung der Arbeitgeber.“ - The procedure reduces employers' work."""

EXAMPLE_ANSWER = CardDraft(
    logic="AND",
    criteria=[
        CriterionDraft(fact="current_certified", op="=", value=True, source_rule_ids=["R-9.9-001"],
                       quote="eine Arbeitsunfähigkeitsbescheinigung vorliegt"),
        CriterionDraft(fact="total_days_12m", op=">", value=42, source_rule_ids=["R-9.9-001"],
                       quote="mehr als 42 Tage umfassen"),
    ],
    unused_rules=[UnusedRule(rule_id="R-9.9-003", reason="Background on the purpose, not a condition.")],
    missing_facts=[MissingFact(rule_ids=["R-9.9-002"], quote="das 25. Lebensjahr vollendet haben",
                               needed="the employee's age in years")],
)


def system_prompt() -> str:
    ops = "\n".join(f"    „{word}“ -> {op}" for word, op in GERMAN_OPERATORS)
    return (SYSTEM.format(operators=ops, facts=facts.describe())
            + f"\n\nEXAMPLE\nInput:\n{EXAMPLE_INPUT}\n\nOutput:\n{EXAMPLE_ANSWER.model_dump_json(indent=1)}")


def render_rules(pick: rule_picker.Pick, rules: list[Requirement]) -> str:
    s = pick.spec
    lines = [f"Card: {s.card_id}  \"{s.title}\""]
    for r in rules:
        flags = f", flags: {' | '.join(r.flags)}" if r.flags else ""
        lines.append(f"{r.id} (p.{r.page}, votes {r.votes}{flags}): „{r.quote}“ - {r.statement}")
    return "\n".join(lines)


# --- running it -----------------------------------------------------------------------------------

class TranslatorError(Exception):
    """The model gave no usable card."""


def build_card(pick: rule_picker.Pick, draft: CardDraft, model: str) -> CriteriaCard:
    """Draft + pick -> CriteriaCard. Raises ValidationError when the card's checks refuse it."""
    criteria = [c.model_dump() | {"id": f"C{n}"} for n, c in enumerate(draft.criteria, start=1)]
    return CriteriaCard.model_validate(pick.skeleton() | {
        "logic": draft.logic,
        "criteria": criteria,
        "unused_rules": [u.model_dump() for u in draft.unused_rules],
        "missing_facts": [m.model_dump() for m in draft.missing_facts],
        "produced_by": model,
        "flags": pick.warnings(),
    })


def _audit(conn: sqlite3.Connection, first_call: int, action: str, subject: str, detail: str = "") -> None:
    calls = llm.calls[first_call:]
    db.log(conn, AuditEntry(
        actor="translator", action=action, subject_id=subject, detail=detail,
        model=calls[-1]["model"] if calls else "",
        cache_hit=bool(calls) and all(c["cache_hit"] for c in calls),
        duration_s=round(sum(c["duration_s"] for c in calls), 2),
    ))


def one_run(conn: sqlite3.Connection, pick: rule_picker.Pick, rules: list[Requirement], model: str,
            attempts: int, label: str) -> CriteriaCard:
    """Ask until the card passes its checks; each refusal is sent back with the reason.

    A card that passes the checks then goes through the guards. Guard errors are sent back ONCE
    (guards are heuristics, so a false alarm must not use up every attempt); whatever is left after
    that is kept on the card as flags for the reviewer.
    """
    system, user = system_prompt(), render_rules(pick, rules)
    last_error = ""
    guards_sent_back = False
    for attempt in range(1, attempts + 1):
        first_call = len(llm.calls)
        try:
            draft = llm.ask_json(model, system, user, CardDraft)
        except ValueError as err:
            _audit(conn, first_call, f"{label} attempt {attempt}: no valid JSON", pick.spec.card_id, str(err))
            raise TranslatorError(f"{model} returned no valid card JSON: {err}") from None
        try:
            card = build_card(pick, draft, model)
        except ValidationError as err:
            last_error = str(err)
            _audit(conn, first_call, f"{label} attempt {attempt}: card refused", pick.spec.card_id, last_error)
            user = (f"{render_rules(pick, rules)}\n\nYour previous card was refused by the checks:\n{last_error}\n"
                    f"Fix these problems and answer again with the complete JSON.")
            continue
        findings = card_guards.check(card, pick.picked)
        errors = [g for g in findings if g.severity == "error"]
        if errors and not guards_sent_back and attempt < attempts:
            guards_sent_back = True
            problems = "\n".join(g.text() for g in errors)
            _audit(conn, first_call, f"{label} attempt {attempt}: {len(errors)} guard error(s), sent back",
                   pick.spec.card_id, problems)
            user = (f"{render_rules(pick, rules)}\n\nYour previous card does not match the rule text:\n{problems}\n"
                    f"Check these against the German quotes and answer again with the complete JSON.")
            continue
        _audit(conn, first_call, f"{label} attempt {attempt}: card with {len(card.criteria)} criteria, "
               f"{len(errors)} guard error(s), {len(findings) - len(errors)} warning(s)", pick.spec.card_id,
               "\n".join(g.text() for g in findings))
        return CriteriaCard.model_validate(card.model_dump() | {"flags": card.flags + [g.text() for g in findings]})
    raise TranslatorError(f"no valid card after {attempts} attempts; last problem:\n{last_error}")


def compare_runs(first: CriteriaCard, second: CriteriaCard) -> list[str]:
    """Differences in meaning between two cards for the same rules, as reviewer flags."""
    m = gold.mark(second, first)
    out = [f"Cross-check (rules in reverse order) disagrees on {c.gold_id} {c.gold}: "
           f"{c.result}{' ' + c.found if c.found else ''}" for c in m.criteria if c.result in ("wrong", "missing")]
    out += [f"Cross-check found an extra condition: {e}" for e in m.extra]
    out += [f"Cross-check: {p}" for p in m.problems]
    for name in ("unused_rules", "missing_facts"):
        a = sorted(getattr(first, name), key=str)
        b = sorted(getattr(second, name), key=str)
        ids_a = sorted(u.rule_id if name == "unused_rules" else ",".join(u.rule_ids) for u in a)
        ids_b = sorted(u.rule_id if name == "unused_rules" else ",".join(u.rule_ids) for u in b)
        if ids_a != ids_b:
            out.append(f"Cross-check: {name} differ ({ids_a} vs {ids_b})")
    return out


def translate(conn: sqlite3.Connection, card_id: str, model: str = config.LLM_MODEL_CODE,
              attempts: int = 3, cross_check: bool = True) -> CriteriaCard:
    pick = rule_picker.pick(conn, card_id)
    card = one_run(conn, pick, pick.picked, model, attempts, "translate")
    flags: list[str] = []
    if cross_check and len(pick.picked) > 1:
        try:
            second = one_run(conn, pick, list(reversed(pick.picked)), model, attempts, "cross-check")
            flags = compare_runs(card, second)
        except TranslatorError as err:
            flags = [f"Cross-check run failed, card is unconfirmed: {str(err).splitlines()[0]}"]
    card = CriteriaCard.model_validate(card.model_dump() | {"flags": card.flags + flags})
    db.log(conn, AuditEntry(actor="translator", action=f"draft card {card.id}: {len(card.criteria)} criteria, "
                            f"{len(flags)} cross-check flag(s)", subject_id=card.id, model=model,
                            detail=f"rules_hash={pick.rules_hash()} content_hash={card.content_hash()}"))
    return card


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description="Translate the picked rules into a draft criteria card.")
    p.add_argument("card", nargs="?", default="REQ-41-PRECONDITION", choices=list(rule_picker.CARDS))
    p.add_argument("--db", help="database file (default: specpilot.db)")
    p.add_argument("--model", default=config.LLM_MODEL_CODE)
    p.add_argument("--no-cross-check", action="store_true")
    p.add_argument("--out", help="save the draft card as JSON, for review (python -m specpilot.review)")
    a = p.parse_args(argv)

    model, path = a.model, a.db
    conn = db.connect(path)
    try:
        card = translate(conn, a.card, model, cross_check=not a.no_cross_check)
    except (rule_picker.PickError, TranslatorError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(card.describe())
    try:
        print("\n" + gold.mark(card, gold.load(card.id)).summary())
    except FileNotFoundError:
        pass
    calls = llm.calls
    print(f"\n{len(calls)} model call(s), {sum(c['cache_hit'] for c in calls)} from cache")
    if a.out:
        Path(a.out).write_text(card.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"Draft card saved to {a.out}. Review it: python -m specpilot.review {a.out} --reviewer \"Name\""
              + (f" --db {path}" if path else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
