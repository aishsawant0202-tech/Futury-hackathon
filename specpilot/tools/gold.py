"""Gold cards: hand-written correct criteria cards, the answer key for the translator.

A gold card is written by a person from the reference model and the spec. It has two jobs:
    1. marking: compare a card the translator (AI) made with the gold card -> how many
       sub-criteria it got right. That turns "the AI seems fine" into a number.
    2. fallback: if the translator fails during the demo, the gold card can be used instead.

Marking compares MEANING, not spelling: for whole-number facts "total_days_12m >= 30" and
"total_days_12m > 29" hold for exactly the same people, so both count as correct
(exact vs equivalent). It is checked by trying every value in the fact's realistic range.

Placeholder rule ids (number 900 and above, e.g. R-3.12-901) mark a gold card written before
the real extraction was available. While a card has them, rule citations are not marked.

Run: python -m specpilot.tools.gold
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

from specpilot import config
from specpilot.criteria import Condition, CriteriaCard
from specpilot.tools import facts
from specpilot.tools.facts import FactType

GOLD_DIR = config.ROOT / "data" / "gold"
PLACEHOLDER_FROM = 900  # R-x.y-9nn = placeholder


# --- loading ---------------------------------------------------------------------------

def load(card_id: str, folder: Path = GOLD_DIR) -> CriteriaCard:
    path = folder / f"{card_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no gold card {card_id} in {folder}; have: {[c.id for c in load_all(folder)]}")
    card = CriteriaCard.model_validate_json(path.read_text(encoding="utf-8"))
    if card.id != card_id:
        raise ValueError(f"{path.name} holds card {card.id}, expected {card_id}")
    return card


def load_all(folder: Path = GOLD_DIR) -> list[CriteriaCard]:
    return [CriteriaCard.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def placeholder_ids(card: CriteriaCard) -> list[str]:
    return [rid for rid in card.source_rule_ids if int(rid.rsplit("-", 1)[1]) >= PLACEHOLDER_FROM]


# --- comparing meaning -------------------------------------------------------------------

def _domain(fact: str) -> list:
    f = facts.get(fact)
    if f.type is FactType.BOOLEAN:
        return [False, True]
    lo, hi = f.plausible
    return list(range(lo - 1, hi + 2))  # one step past each end too


def same_meaning(a: Condition, b: Condition) -> bool:
    """True if both conditions are about the same fact and hold for exactly the same values."""
    if a.fact != b.fact:
        return False
    return all(facts.holds(v, a.op, a.value) == facts.holds(v, b.op, b.value) for v in _domain(a.fact))


def _exact(a: Condition, b: Condition) -> bool:
    return (a.fact, a.op, a.value) == (b.fact, b.op, b.value)


# --- marking -------------------------------------------------------------------------------

@dataclass
class CriterionMark:
    gold_id: str
    gold: str  # gold condition as text
    result: str  # exact | equivalent | wrong | missing
    found: str = ""  # candidate condition as text, with its C id
    note: str = ""


@dataclass
class Marking:
    card_id: str
    criteria: list[CriterionMark] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)  # candidate criteria with no gold counterpart
    problems: list[str] = field(default_factory=list)  # card-level differences (logic, reason codes, ...)
    citations_checked: bool = True

    @property
    def correct(self) -> int:
        return sum(m.result in ("exact", "equivalent") for m in self.criteria)

    @property
    def all_correct(self) -> bool:
        return self.correct == len(self.criteria) and not self.extra and not self.problems

    def summary(self) -> str:
        head = (f"{self.card_id}: {self.correct}/{len(self.criteria)} sub-criteria correct, "
                f"{len(self.extra)} extra, {len(self.problems)} other difference(s)"
                f" -> {'ALL CORRECT' if self.all_correct else 'NOT CORRECT'}")
        lines = [head]
        for m in self.criteria:
            found = f" | candidate {m.found}" if m.found else ""
            note = f" ({m.note})" if m.note else ""
            lines.append(f"  {m.result:<10} gold {m.gold_id} {m.gold}{found}{note}")
        lines += [f"  extra      {e}" for e in self.extra]
        lines += [f"  problem    {p}" for p in self.problems]
        if not self.citations_checked:
            lines.append("  (rule citations not marked: the gold card has placeholder rule ids)")
        return "\n".join(lines)


def _readings(c) -> list[Condition]:
    return [c, *c.alternatives]


def mark(candidate: CriteriaCard, gold: CriteriaCard) -> Marking:
    """Score a translator-made card against the gold card."""
    m = Marking(card_id=gold.id, citations_checked=not placeholder_ids(gold))

    for attr in ("logic", "kind"):
        if getattr(candidate, attr) != getattr(gold, attr):
            m.problems.append(f"{attr} is {getattr(candidate, attr)!r}, gold says {getattr(gold, attr)!r}")
    if sorted(candidate.reason_codes) != sorted(gold.reason_codes):
        m.problems.append(f"reason codes {candidate.reason_codes}, gold says {gold.reason_codes}")

    unmatched = list(candidate.criteria)
    for g in gold.criteria:
        same_fact = [c for c in unmatched if c.fact == g.fact]
        if not same_fact:
            m.criteria.append(CriterionMark(g.id, g.describe(), "missing"))
            continue
        # prefer the candidate criterion that means the same; else the first on that fact
        c = next((c for c in same_fact if same_meaning(c, g)), same_fact[0])
        unmatched.remove(c)
        if _exact(c, g):
            result = "exact"
        elif same_meaning(c, g):
            result = "equivalent"
        else:
            result = "wrong"
        notes = []
        g_alts = [a for a in g.alternatives]
        c_alts = [a for a in c.alternatives]
        if len(g_alts) != len(c_alts) or not all(any(same_meaning(ca, ga) for ca in c_alts) for ga in g_alts):
            notes.append(f"alternative readings differ: gold {[a.describe() for a in g_alts]}, "
                         f"candidate {[a.describe() for a in c_alts]}")
            if result != "wrong":
                result = "wrong"
        if m.citations_checked and sorted(c.source_rule_ids) != sorted(g.source_rule_ids):
            notes.append(f"cites {c.source_rule_ids}, gold cites {g.source_rule_ids}")
        m.criteria.append(CriterionMark(g.id, g.describe(), result, f"{c.id} {c.describe()}", "; ".join(notes)))

    m.extra = [f"{c.id} {c.describe()}" for c in unmatched]
    return m


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    for card in load_all():
        print(card.describe())
        ph = placeholder_ids(card)
        if ph:
            print(f"\n  {len(ph)} placeholder rule id(s): {', '.join(ph)}")
        print()


if __name__ == "__main__":
    main()
