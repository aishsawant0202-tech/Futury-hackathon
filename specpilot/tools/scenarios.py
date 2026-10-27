"""Content scenarios card: test design that places dummy people on the DBVO content edges.

The precondition card decides WHETHER the message is sent; it marks the content rules (the 6-month chain, the
12-month limit) as unused. This builder turns those rules into a card whose conditions are chain facts:

    C1  chain_link_exactly_6m = true    two earlier illnesses exactly 6 months apart  (p.135 vs p.136)
    C2  prior_ends_before_12m = true    an earlier illness before the 12-month window (no limit vs 12 months)
    C3  uncertified_inside = true       an absence without a sick note inside the chain (it must be skipped)
    C4  prior_gaps_under_180d = true    a chain that never breaks

so the branch maker gives 2^4 = 16 combinations. Every person must ALSO meet all send conditions of the
precondition card (given to the branch maker as --background), so every person has a message whose content
matters. Each condition cites the content rule it exercises, with that rule's exact German text. The card is
a DRAFT flagged as test design; it is reviewed and approved like any other card.

Run: python -m specpilot.tools.scenarios card.json --db specpilot.db --out scenarios.json
"""
import argparse
import sys
from pathlib import Path

from specpilot import db
from specpilot.criteria import CriteriaCard
from specpilot.models import Requirement, RuleKind
from specpilot.tools import card_guards, quote_check

VERSION = "scenario builder 0.1"


class ScenarioError(Exception):
    """The scenarios card cannot be built; the message says why."""


def _mentions(rule: Requirement, words: str) -> bool:
    return quote_check.compact(words) in quote_check.compact(rule.quote)


def build(precondition: CriteriaCard, rules: list[Requirement]) -> CriteriaCard:
    """A draft content scenarios card from an approved precondition card and the extracted rules."""
    by_id = {r.id: r for r in rules}
    content = [by_id[u.rule_id] for u in precondition.unused_rules if u.rule_id in by_id]
    missing = [u.rule_id for u in precondition.unused_rules if u.rule_id not in by_id]
    if missing:
        raise ScenarioError(f"rules {missing} are not in the database")
    six = [r for r in content if _mentions(r, "6 Monate")]
    twelve = [r for r in content if _mentions(r, "12 Monate")]
    if not six:
        raise ScenarioError(f"none of {precondition.id}'s unused rules is about the 6-month chain ('6 Monate')")
    if not twelve:
        raise ScenarioError(f"none of {precondition.id}'s unused rules is about the 12-month limit ('12 Monate')")
    no_limit = [r for r in content if _mentions(r, "keine zeitliche Begrenzung") and r not in twelve]

    def crit(cid, fact, cited):
        return {"id": cid, "fact": fact, "op": "=", "value": True, "source_rule_ids": [r.id for r in cited],
                "quote": cited[0].quote}

    criteria = [crit("C1", "chain_link_exactly_6m", six), crit("C2", "prior_ends_before_12m", twelve + no_limit),
                crit("C3", "uncertified_inside", six), crit("C4", "prior_gaps_under_180d", six)]
    cited = {rid for c in criteria for rid in c["source_rule_ids"]}
    code = precondition.reason_codes[0]
    card = CriteriaCard.model_validate({
        "id": f"REQ-{code}-CONTENT-SCENARIOS",
        "title": f"Where do reason {code} people sit relative to the DBVO content rules? (test design)",
        "reason_codes": precondition.reason_codes, "kind": RuleKind.CONTENT.value, "logic": "AND",
        "criteria": criteria, "source_rule_ids": [r.id for r in content],
        "unused_rules": [{"rule_id": r.id, "reason": "not used for a content scenario (test design)"}
                         for r in content if r.id not in cited],
        "document": precondition.document, "produced_by": VERSION,
        "flags": [f"TEST DESIGN: conditions chosen to reach the DBVO content edges, not wording from the spec. "
                  f"Every person must also meet the send conditions of {precondition.id} (branch maker --background)."],
    })
    found = [g.text() for g in card_guards.check(card, rules)]
    return CriteriaCard.model_validate(card.model_dump() | {"flags": card.flags + found})


def main(argv: list[str] | None = None) -> int:
    from specpilot.review import rules_for, save_card
    from specpilot.tools.branch_maker import BranchError, load_card
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Build a draft content scenarios card from an approved precondition card.")
    p.add_argument("card", help="the APPROVED precondition card")
    p.add_argument("--db", required=True, help="database with the extracted rules")
    p.add_argument("--out", required=True, help="where to save the draft scenarios card")
    a = p.parse_args(argv)
    try:
        precondition = load_card(Path(a.card))
        card = build(precondition, rules_for(db.connect(a.db), precondition))
    except (BranchError, ScenarioError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    save_card(card, Path(a.out))
    print(card.describe())
    print(f"\nDraft saved to {a.out}. Next:\n"
          f"  python -m specpilot.review {a.out} --reviewer \"Name\" --db {a.db}\n"
          f"  python -m specpilot.tools.branch_maker {a.out} --background {a.card}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
