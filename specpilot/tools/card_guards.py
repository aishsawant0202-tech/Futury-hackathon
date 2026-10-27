"""Card guards: plain-code checks that compare a translator card with the German rule text.

The card form (specpilot/criteria.py) already refuses cards that are malformed: unknown facts, wrong
types, uncited or unexplained rules. The guards catch cards that are well-formed but do not say what
the rules say:

  error    quote_in_rule     the quote is not words from the cited rule (made-up evidence)
  error    limit             the German limit words disagree with the condition
                             ("mindestens 30 Tage" but the card says > 30, or 31)
  warning  limit_unchecked   no limit word or no number in the quote, so the condition cannot be checked
  warning  period            the quote names a month period the fact does not use ("12 Monate" on a 6-month fact)
  warning  fact_terms        the quote contains none of the German words known for that fact
  warning  plausible         a value outside the fact's realistic range
  warning  same_fact         two criteria on one fact (some combinations may be impossible)
  warning  contradiction     a cited rule is flagged as contradicting another, but the criterion has one reading
  warning  missing_fact      the card needs a fact the dictionary does not have yet

Errors are sent back to the translator once; whatever remains goes on the card as flags for the reviewer.
These are heuristics: they point a person at the right place, they do not decide.
"""
import re
from dataclasses import dataclass
from typing import Literal

from specpilot.criteria import Condition, CriteriaCard
from specpilot.models import Requirement
from specpilot.tools import facts, gold, quote_check
from specpilot.tools.facts import FactType
from specpilot.tools.reference_model import GAP_MONTHS, SUM_MONTHS

# German limit words -> operator. Longer phrases first: "nicht mehr als" must win over "mehr als".
GERMAN_OPERATORS: list[tuple[str, str]] = [
    ("nicht weniger als", ">="), ("nicht mehr als", "<="), ("mindestens", ">="), ("wenigstens", ">="),
    ("höchstens", "<="), ("maximal", "<="), ("bis zu", "<="), ("mehr als", ">"), ("über", ">"),
    ("weniger als", "<"), ("unter", "<"), ("genau", "="),
]

# Month periods built into a fact's definition (facts.py / reference_model.py)
FACT_MONTHS: dict[str, set[int]] = {
    "prior_certified_6m": {GAP_MONTHS},
    "total_days_12m": {SUM_MONTHS},
    "latest_gap_within_6m": {GAP_MONTHS},
    "chain_link_exactly_6m": {GAP_MONTHS},
    "prior_gaps_under_180d": {GAP_MONTHS},
    "prior_ends_before_12m": {SUM_MONTHS},
}

UNITS = {"tag": "days", "kalendertag": "days", "arbeitstag": "days", "monat": "months", "woche": "weeks", "jahr": "years"}


@dataclass(frozen=True)
class GuardFinding:
    severity: Literal["error", "warning"]
    guard: str
    where: str  # "C3", "C3 reading 'p.136'" or "card"
    message: str

    def text(self) -> str:
        return f"[{self.guard}] {self.where}: {self.message}"


@dataclass(frozen=True)
class Limit:
    """A limit stated in German, e.g. 'mindestens 30 Tage' -> >= 30 days."""

    words: str
    op: str
    number: int
    unit: str  # days | months | weeks | years | "" (no unit word)


def _unit(word: str) -> str:
    word = word.lower()
    for stem, unit in UNITS.items():
        if word.startswith(stem):
            return unit
    return ""


_NUMBER = re.compile(r"(?<![\w.])(\d+)(?:\s+([A-Za-zÄÖÜäöüß]+))?")


def numbers(text: str) -> list[tuple[int, str]]:
    """Every whole number in the text with the unit word after it: '30 Tage' -> (30, 'days')."""
    return [(int(n), _unit(w or "")) for n, w in _NUMBER.findall(text)]


def limits(text: str) -> list[Limit]:
    """Limit phrases in a German text: a limit word, up to three words, then a number (and its unit)."""
    t = re.sub(r"\s+", " ", text.lower())
    found, taken = [], []
    for word, op in GERMAN_OPERATORS:
        pattern = rf"(?<!\w){re.escape(word)}(?!\w)\s+(?:[^\W\d]+\s+){{0,3}}?(\d+)(?:\s+([^\W\d]+))?"
        for m in re.finditer(pattern, t):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue  # "mehr als 6" inside "nicht mehr als 6" is already taken
            taken.append((m.start(), m.end()))
            found.append((m.start(), Limit(m.group(0), op, int(m.group(1)), _unit(m.group(2) or ""))))
    return [lim for _, lim in sorted(found, key=lambda x: x[0])]


def _fragments(quote: str) -> list[str]:
    """A quote may skip words with '...' or '…': each part must be in the rule, in order."""
    return [p for p in (s.strip() for s in re.split(r"\.\.\.|…", quote)) if len(quote_check.compact(p)) >= 3]


def quote_in(quote: str, rule_quote: str) -> bool:
    haystack, pos = quote_check.compact(rule_quote), 0
    for part in _fragments(quote):
        i = haystack.find(quote_check.compact(part), pos)
        if i < 0:
            return False
        pos = i + len(quote_check.compact(part))
    return True


@dataclass(frozen=True)
class _Stated:
    """A condition as the German words state it, for comparing meaning with gold.same_meaning."""
    fact: str
    op: str
    value: int


def _check_condition(cond: Condition, where: str, rules: dict[str, Requirement]) -> list[GuardFinding]:
    out: list[GuardFinding] = []
    f = facts.get(cond.fact)
    cited = [rules[r] for r in cond.source_rule_ids if r in rules]

    # 1. the quote must be words from a cited rule
    if cited and not any(quote_in(cond.quote, r.quote) for r in cited):
        out.append(GuardFinding("error", "quote_in_rule", where,
                                f"quote „{cond.quote}“ is not in {', '.join(r.id for r in cited)}; "
                                f"copy the German words from that rule"))

    # 2. limit words and numbers (number facts)
    if f.type is FactType.INTEGER:
        wanted_unit = f.unit
        stated = [lim for lim in limits(cond.quote) if lim.unit in (wanted_unit, "")]
        nums = [n for n, unit in numbers(cond.quote) if unit in (wanted_unit, "")]
        values = cond.value if isinstance(cond.value, list) else [cond.value]
        if stated:
            if not any(gold.same_meaning(cond, _Stated(cond.fact, lim.op, lim.number)) for lim in stated):
                said = ", ".join(f"„{lim.words}“ ({lim.op} {lim.number})" for lim in stated)
                out.append(GuardFinding("error", "limit", where,
                                        f"card says {cond.describe()}, but the quote says {said}"))
        elif nums:
            if not all(v in nums for v in values):
                out.append(GuardFinding("error", "limit", where,
                                        f"card value {cond.value} is not a number in the quote (it has {nums})"))
            else:
                out.append(GuardFinding("warning", "limit_unchecked", where,
                                        "no limit word (mindestens, mehr als, ...) in the quote: check the operator"))
        else:
            out.append(GuardFinding("warning", "limit_unchecked", where,
                                    f"no number in {wanted_unit} in the quote: check value and operator by hand"))
        lo, hi = f.plausible
        if any(not lo <= v <= hi for v in values):
            out.append(GuardFinding("warning", "plausible", where,
                                    f"value {cond.value} is outside the realistic range {lo}-{hi} {f.unit}"))

    # 3. month periods the fact does not use
    months = {n for n, unit in numbers(cond.quote) if unit == "months"}
    own = FACT_MONTHS.get(cond.fact, set())
    if months and not months & own:
        uses = f"uses {', '.join(map(str, sorted(own)))} months" if own else "has no month period"
        out.append(GuardFinding("warning", "period", where,
                                f"quote names {', '.join(map(str, sorted(months)))} month(s) but {cond.fact} {uses}"))

    # 4. the quote should mention the fact somehow
    text = quote_check.compact(cond.quote)
    if not any(quote_check.compact(term) in text for term in f.german_terms):
        out.append(GuardFinding("warning", "fact_terms", where,
                                f"quote contains none of the words known for {cond.fact} "
                                f"({'; '.join(f.german_terms)}): check the fact is the right one"))
    return out


def check(card: CriteriaCard, rules: list[Requirement]) -> list[GuardFinding]:
    """All findings for a card, errors first. `rules` are the picked rules the card was built from."""
    by_id = {r.id: r for r in rules}
    out: list[GuardFinding] = []
    for c in card.criteria:
        out += _check_condition(c, c.id, by_id)
        for alt in c.alternatives:
            out += _check_condition(alt, f"{c.id} reading '{alt.label}'", by_id)
        if not c.alternatives:
            flagged = [by_id[r] for r in c.source_rule_ids if r in by_id and any("Contradiction" in f for f in by_id[r].flags)]
            for r in flagged:
                out.append(GuardFinding("warning", "contradiction", c.id,
                                        f"{r.id} is flagged ({'; '.join(f for f in r.flags if 'Contradiction' in f)}) "
                                        f"but {c.id} has only one reading: does it need an alternative?"))

    seen: dict[str, str] = {}
    for c in card.criteria:
        if c.fact in seen:
            out.append(GuardFinding("warning", "same_fact", f"{seen[c.fact]}+{c.id}",
                                    f"both are about {c.fact}: check they fit together; some combinations "
                                    f"of the two may be impossible"))
        seen.setdefault(c.fact, c.id)
    for m in card.missing_facts:
        out.append(GuardFinding("warning", "missing_fact", "card",
                                f"{', '.join(m.rule_ids)} needs a fact the dictionary lacks ({m.needed}); "
                                f"the card is incomplete until it is added"))
    return sorted(out, key=lambda g: g.severity != "error")
