"""Review of criteria cards: a person approves, rejects or corrects what the translator proposed.

Run: python -m specpilot.review card.json --reviewer "Name" [--db path] [--out file]

The review sheet shows every sub-criterion as a plain sentence next to the German quote it rests on,
with the full rule text and every warning. The reviewer can
    approve   locks the approval to the exact content (any later change voids it, see criteria.py)
    reject    with a reason
    edit      change a criterion's fact, operator, value or quote -> new version, back to draft
    remove    drop a criterion; its rules move to "unused" with the reviewer's reason
    reading   resolve an ambiguity by choosing one reading
Every action is checked by the card's own rules, re-checked by the guards when the rules are at hand,
recorded on the card (flags starting "review:") and, with a database, in the audit log.

Approval is refused while the card still has missing facts or placeholder rule ids, and while it
has guard errors, unless the reviewer explicitly accepts them.

The functions below hold all the logic; the terminal loop at the bottom only asks and prints, so a
Streamlit screen can use the same functions later.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from specpilot import db
from specpilot.criteria import CardStatus, Condition, CriteriaCard, check_condition
from specpilot.models import AuditEntry, Requirement
from specpilot.tools import card_guards, facts, gold
from specpilot.tools.facts import FactType


class ReviewError(Exception):
    """The action is not allowed; the message says why. The card is unchanged."""


# --- reading the card -----------------------------------------------------------------------

_OP_WORDS = {"=": "exactly", "!=": "anything but", ">": "more than", ">=": "at least", "<": "less than",
             "<=": "at most"}


def plain(cond: Condition) -> str:
    """A condition as an English sentence, for the reviewer."""
    f = facts.get(cond.fact)
    if f.type is FactType.BOOLEAN:
        yes = cond.value if cond.op == "=" else not cond.value
        return f"{f.meaning} -> must be {'YES' if yes else 'NO'}"
    if cond.op == "between":
        return f"{f.meaning} -> must be between {cond.value[0]} and {cond.value[1]} {f.unit} (both included)"
    return f"{f.meaning} -> must be {_OP_WORDS[cond.op]} {cond.value} {f.unit}"


_GUARD_FLAG = re.compile(r"^\[(\w+)\] (C\d+(?:\+C\d+)*)\b")


def _guard_flags_for(card: CriteriaCard, cid: str) -> list[str]:
    """Guard flags about one criterion: '[limit] C3: ...', '[x] C3 reading ...', '[same_fact] C3+C6: ...'."""
    out = []
    for f in card.flags:
        m = _GUARD_FLAG.match(f)
        if m and cid in m.group(2).split("+"):
            out.append(f)
    return out


def guard_errors(card: CriteriaCard) -> list[str]:
    error_guards = {"quote_in_rule", "limit"}
    return [f for f in card.flags if f.startswith("[") and f[1:f.index("]")] in error_guards]


def review_sheet(card: CriteriaCard, rules: list[Requirement] | None = None) -> str:
    by_id = {r.id: r for r in rules or []}
    lines = [f"REVIEW  {card.id}  v{card.version}  [{card.status.value}]  made by {card.produced_by or '?'}",
             f"        {card.title}",
             f"        All conditions must hold ({card.logic})" if card.logic == "AND"
             else f"        At least one condition must hold ({card.logic})", ""]
    for c in card.criteria:
        lines.append(f"  {c.id}  {c.describe()}")
        lines.append(f"      means:  {plain(c)}")
        lines.append(f"      quote:  „{c.quote}“")
        for rid in c.source_rule_ids:
            r = by_id.get(rid)
            lines.append(f"      rule:   {rid} p.{r.page}: „{r.quote}“" if r else f"      rule:   {rid}")
        for a in c.alternatives:
            lines.append(f"      OR reading '{a.label}': {a.describe()}  -  {plain(a)}  „{a.quote}“")
        if c.alternatives:
            lines.append(f"      main reading '{c.label}'; ambiguous because: {c.ambiguity}")
        for f in _guard_flags_for(card, c.id):
            lines.append(f"      ⚑ {f}")
        lines.append("")
    if card.unused_rules:
        lines.append("  Not used as conditions:")
        for u in card.unused_rules:
            r = by_id.get(u.rule_id)
            text = f" p.{r.page}: „{r.quote}“" if r else ""
            lines.append(f"    {u.rule_id}{text}\n      reason: {u.reason}")
        lines.append("")
    for m in card.missing_facts:
        lines.append(f"  ⚑ MISSING FACT for {', '.join(m.rule_ids)}: {m.needed}  („{m.quote}“)")
    shown = {f for c in card.criteria for f in _guard_flags_for(card, c.id)}
    other = [f for f in card.flags if f not in shown]
    if other:
        lines.append("  Notes and warnings:")
        lines += [f"    ⚑ {f}" for f in other]
    if card.status is not CardStatus.DRAFT:
        lines.append(f"\n  {card.status.value.upper()} by {card.reviewer} at {card.reviewed_at:%Y-%m-%d %H:%M}"
                     + (f": {card.review_note}" if card.review_note else ""))
    return "\n".join(lines)


# --- actions -------------------------------------------------------------------------------------

def _rebuild(card: CriteriaCard, data: dict, note: str, rules: list[Requirement]) -> CriteriaCard:
    """Validate the edited card as a new draft version and re-run the guards (old guard flags are replaced)."""
    data = data | {"version": card.version + 1, "status": "draft", "reviewer": "", "reviewed_at": None,
                   "review_note": "", "approved_hash": ""}
    data["flags"] = [f for f in data["flags"] if not f.startswith("[")] + [f"review: {note}"]
    try:
        new = CriteriaCard.model_validate(data)
    except ValidationError as err:
        raise ReviewError(f"edit refused: {_first_error(err)}") from None
    found = [g.text() for g in card_guards.check(new, rules)]
    return CriteriaCard.model_validate(new.model_dump() | {"flags": new.flags + found})


def _need_rules(rules: list[Requirement] | None) -> list[Requirement]:
    if not rules:
        raise ReviewError("editing needs the rule texts so the guards can re-check the card: run with --db")
    return rules


def _first_error(err: ValidationError) -> str:
    e = err.errors()[0]
    return e["msg"].removeprefix("Value error, ")


def _criterion_index(card: CriteriaCard, cid: str) -> int:
    for i, c in enumerate(card.criteria):
        if c.id == cid:
            return i
    raise ReviewError(f"{card.id} has no criterion {cid}; it has {', '.join(c.id for c in card.criteria)}")


def _need_draft(card: CriteriaCard, action: str) -> None:
    if card.status is CardStatus.REJECTED:
        raise ReviewError(f"cannot {action} a rejected card; translate again")


def refresh_guards(card: CriteriaCard, rules: list[Requirement]) -> CriteriaCard:
    """Replace the guard flags with a fresh check of the card as it is now.
    Stored flags may be stale: the file could have been changed after the translator wrote them."""
    flags = [f for f in card.flags if not f.startswith("[")] + [g.text() for g in card_guards.check(card, rules)]
    return CriteriaCard.model_validate(card.model_dump() | {"flags": flags})


def approve(card: CriteriaCard, reviewer: str, rules: list[Requirement] | None, note: str = "",
            accept_guard_errors: bool = False, now: datetime | None = None) -> CriteriaCard:
    """Approve a draft. The guards are re-run on the card as it is now, never taken from stored flags."""
    if not reviewer.strip():
        raise ReviewError("approval needs the reviewer's name")
    if card.status is not CardStatus.DRAFT:
        raise ReviewError(f"only a draft can be approved; this card is {card.status.value}")
    if card.missing_facts:
        raise ReviewError("the card has missing facts: add them to the fact dictionary, or remove the condition "
                          "and record why, before approving")
    placeholders = gold.placeholder_ids(card)
    if placeholders:
        raise ReviewError(f"placeholder rule ids {placeholders} cannot be approved; use the real extracted rules")
    if not rules:
        raise ReviewError("approval needs the rule texts so the guards can check the card as it is now: run with --db")
    missing = [rid for rid in card.source_rule_ids if rid not in {r.id for r in rules}]
    if missing:
        raise ReviewError(f"rules {missing} are not in the database, so the card cannot be checked")
    card = refresh_guards(card, rules)
    errors = guard_errors(card)
    if errors and not accept_guard_errors:
        raise ReviewError("the card still has guard errors; fix them, or approve explicitly accepting them:\n"
                          + "\n".join(f"  {e}" for e in errors))
    if errors:
        note = (note + " | " if note else "") + f"approved accepting {len(errors)} guard error(s)"
    data = card.model_dump() | {"status": "approved", "reviewer": reviewer.strip(), "reviewed_at": now or datetime.now(),
                                "review_note": note, "approved_hash": card.content_hash()}
    return CriteriaCard.model_validate(data)


def reject(card: CriteriaCard, reviewer: str, reason: str, now: datetime | None = None) -> CriteriaCard:
    if not reviewer.strip() or not reason.strip():
        raise ReviewError("rejection needs the reviewer's name and a reason")
    if card.status is not CardStatus.DRAFT:
        raise ReviewError(f"only a draft can be rejected; this card is {card.status.value}")
    data = card.model_dump() | {"status": "rejected", "reviewer": reviewer.strip(), "reviewed_at": now or datetime.now(),
                                "review_note": reason.strip()}
    return CriteriaCard.model_validate(data)


def edit_criterion(card: CriteriaCard, cid: str, reviewer: str, rules: list[Requirement] | None = None,
                   **changes) -> CriteriaCard:
    """Change fact / op / value / quote of one criterion. Returns a new draft version."""
    _need_draft(card, "edit")
    rules = _need_rules(rules)
    allowed = {"fact", "op", "value", "quote"}
    if not changes or set(changes) - allowed:
        raise ReviewError(f"edit one or more of {sorted(allowed)}")
    i = _criterion_index(card, cid)
    data = card.model_dump()
    data["criteria"][i] |= changes
    c = data["criteria"][i]
    try:
        check_condition(c["fact"], c["op"], c["value"])  # clear message before the card's own checks
    except ValueError as err:
        raise ReviewError(f"edit refused: {err}") from None
    note = (f"{reviewer} edited {cid}: {card.criteria[i].describe()} -> {c['fact']} {c['op']} {json.dumps(c['value'])}"
            + (f" (quote now „{c['quote']}“)" if "quote" in changes else ""))
    return _rebuild(card, data, note, rules)


def remove_criterion(card: CriteriaCard, cid: str, reviewer: str, reason: str,
                     rules: list[Requirement] | None = None) -> CriteriaCard:
    """Drop a criterion. Rules no other criterion cites move to unused_rules with the reason."""
    _need_draft(card, "edit")
    rules = _need_rules(rules)
    if not reason.strip():
        raise ReviewError("removing a criterion needs a reason")
    i = _criterion_index(card, cid)
    if len(card.criteria) == 1:
        raise ReviewError("a card needs at least one criterion; reject the card instead")
    data = card.model_dump()
    gone = data["criteria"].pop(i)
    for n, c in enumerate(data["criteria"], start=1):
        c["id"] = f"C{n}"
    still = {r for c in data["criteria"] for cond in (c, *c["alternatives"]) for r in cond["source_rule_ids"]}
    for rid in gone["source_rule_ids"]:
        if rid not in still:
            data["unused_rules"].append({"rule_id": rid, "reason": f"reviewer removed the condition: {reason.strip()}"})
    note = f"{reviewer} removed {cid} ({card.criteria[i].describe()}): {reason.strip()}; later criteria renumbered"
    return _rebuild(card, data, note, rules)


def choose_reading(card: CriteriaCard, cid: str, label: str, reviewer: str,
                   rules: list[Requirement] | None = None) -> CriteriaCard:
    """Resolve an ambiguity: the chosen reading becomes the only one."""
    _need_draft(card, "edit")
    rules = _need_rules(rules)
    i = _criterion_index(card, cid)
    c = card.criteria[i]
    if not c.alternatives:
        raise ReviewError(f"{cid} has only one reading")
    try:
        chosen = c.reading(label)
    except KeyError as err:
        raise ReviewError(err.args[0]) from None
    data = card.model_dump()
    data["criteria"][i] = {"id": cid, "fact": chosen.fact, "op": chosen.op, "value": chosen.value,
                           "source_rule_ids": list(chosen.source_rule_ids), "quote": chosen.quote,
                           "label": "", "ambiguity": "", "alternatives": []}
    still = {r for c2 in data["criteria"] for cond in (c2, *c2["alternatives"]) for r in cond["source_rule_ids"]}
    for alt in [c, *c.alternatives]:
        for rid in alt.source_rule_ids:
            if rid not in still and rid not in {u["rule_id"] for u in data["unused_rules"]}:
                data["unused_rules"].append({"rule_id": rid, "reason": f"reviewer chose reading '{label}' for {cid}"})
    note = f"{reviewer} chose reading '{label}' for {cid} ({chosen.describe()}); ambiguity was: {c.ambiguity}"
    return _rebuild(card, data, note, rules)


def last_review_note(card: CriteriaCard) -> str:
    notes = [f for f in card.flags if f.startswith("review: ")]
    return notes[-1].removeprefix("review: ") if notes else ""


def log_action(conn: sqlite3.Connection | None, card: CriteriaCard, action: str) -> None:
    if conn is not None:
        db.log(conn, AuditEntry(actor="examiner", action=action, subject_id=card.id,
                                detail=f"v{card.version} {card.status.value} content_hash={card.content_hash()}"))


# --- files ----------------------------------------------------------------------------------------

def load_card(path: Path) -> CriteriaCard:
    return CriteriaCard.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_card(card: CriteriaCard, path: Path) -> None:
    Path(path).write_text(card.model_dump_json(indent=2) + "\n", encoding="utf-8")


def rules_for(conn: sqlite3.Connection, card: CriteriaCard) -> list[Requirement]:
    rules = [db.get_requirement(conn, rid) for rid in card.source_rule_ids]
    return [r for r in rules if r is not None]


# --- terminal ----------------------------------------------------------------------------------

MENU = ("\n[a] approve   [r] reject   [e] edit a criterion   [x] remove a criterion   "
        "[p] pick a reading   [s] show again   [q] save and quit   [!] quit WITHOUT saving")


def run(card: CriteriaCard, reviewer: str, rules: list[Requirement] | None, conn: sqlite3.Connection | None,
        ask: Callable[[str], str] | None = None, say: Callable[[str], None] | None = None) -> CriteriaCard | None:
    """Interactive review. Returns the card to save, or None to discard the changes."""
    raw_ask = ask or input
    say = say or print

    def ask(prompt: str) -> str:
        # Windows PowerShell may put an invisible byte-order mark before piped input; drop it
        return raw_ask(prompt).replace("﻿", "")
    if rules:
        card = refresh_guards(card, rules)  # show what is wrong with the card as it is now
    else:
        say("No --db given: the rule texts are not shown, and editing and approving are not possible.\n")
    say(review_sheet(card, rules))
    while True:
        say(MENU)
        choice = ask("> ").strip().lower()
        try:
            if choice == "a":
                errors = guard_errors(refresh_guards(card, rules)) if rules else []
                accept = False
                if errors:
                    say("Guard errors on this card:\n" + "\n".join(f"  {e}" for e in errors))
                    accept = ask("Approve anyway, accepting them? Type YES: ").strip() == "YES"
                    if not accept:
                        continue
                card = approve(card, reviewer, rules, ask("Note (optional): ").strip(), accept_guard_errors=accept)
                log_action(conn, card, f"approved {card.id} v{card.version}")
                say(f"Approved {card.id} v{card.version}.")
            elif choice == "r":
                card = reject(card, reviewer, ask("Reason: "))
                log_action(conn, card, f"rejected {card.id} v{card.version}: {card.review_note}")
                say(f"Rejected {card.id}.")
            elif choice == "e":
                cid = ask("Criterion (e.g. C3): ").strip().upper()
                field = ask("Change what (fact / op / value / quote): ").strip().lower()
                raw = ask("New value: ").strip()
                value = json.loads(raw) if field == "value" else raw
                card = edit_criterion(card, cid, reviewer, rules, **{field: value})
                log_action(conn, card, last_review_note(card))
                say(review_sheet(card, rules))
            elif choice == "x":
                cid = ask("Criterion to remove: ").strip().upper()
                card = remove_criterion(card, cid, reviewer, ask("Reason: "), rules)
                log_action(conn, card, f"removed {cid} from {card.id}")
                say(review_sheet(card, rules))
            elif choice == "p":
                cid = ask("Criterion: ").strip().upper()
                card = choose_reading(card, cid, ask("Reading label: ").strip(), reviewer, rules)
                log_action(conn, card, f"chose a reading for {cid} on {card.id}")
                say(review_sheet(card, rules))
            elif choice == "s":
                say(review_sheet(card, rules))
            elif choice == "q":
                return card
            elif choice == "!":
                return None
            else:
                say("Unknown choice.")
        except ReviewError as err:
            say(f"Not done: {err}")
        except json.JSONDecodeError:
            say("Not done: a value must be true, false, a whole number or [low, high].")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Review a draft criteria card.")
    p.add_argument("card", help="card JSON file (python -m specpilot.agents.translator --out card.json)")
    p.add_argument("--reviewer", required=True, help="your name, recorded with every action")
    p.add_argument("--db", help="database with the extracted rules: shows the full rule texts and re-runs the guards")
    p.add_argument("--out", help="where to save (default: overwrite the card file)")
    a = p.parse_args(argv)
    if _is_content_card(Path(a.card)):
        return _review_content(Path(a.card), a.reviewer, Path(a.out or a.card))
    try:
        card = load_card(Path(a.card))
    except (OSError, ValidationError) as err:
        print(f"error: cannot load {a.card}: {err}", file=sys.stderr)
        return 1
    conn = db.connect(a.db) if a.db else None
    rules = rules_for(conn, card) if conn else None
    result = run(card, a.reviewer, rules, conn)
    if result is None:
        print("Quit without saving.")
        return 0
    out = Path(a.out or a.card)
    save_card(result, out)
    print(f"Saved {result.id} v{result.version} ({result.status.value}) to {out}")
    return 0


def _is_content_card(path: Path) -> bool:
    """A DBVO content card has "rules" and "fields"; a criteria card has "criteria"."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "rules" in data and "fields" in data and "criteria" not in data


def _review_content(path: Path, reviewer: str, out: Path) -> int:
    from specpilot import content_review, dbvo
    try:
        card = dbvo.load_card(path)
    except ValidationError as err:
        print(f"error: cannot load {path}: {err.errors()[0]['msg'].removeprefix('Value error, ')}", file=sys.stderr)
        return 1
    result = content_review.run(card, reviewer)
    if result is None:
        print("Quit without saving.")
        return 0
    dbvo.save_card(result, out)
    print(f"Saved {result.card_id} v{result.version} ({result.status.value}) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
