"""Review of a DBVO content card: you approve, reject or correct the content rules before they are used.

Started through the normal review command (python -m specpilot.review content_card.json --reviewer "Name"),
which recognises a content card by itself. The sheet shows every content rule with its value per reading
and its sources, and every field with its format. You can
    approve   locked to the exact content; with placeholder sources only after typing YES, and the number of
              accepted placeholders is recorded on the card
    reject    with a reason
    edit      change one rule's value for one reading -> new version, back to draft (approval voided)
Every action is recorded on the card (flags starting "review:").
"""
import json
from datetime import datetime
from typing import Callable

from pydantic import ValidationError

from specpilot.criteria import CardStatus
from specpilot.dbvo import ContentCard
from specpilot.review import ReviewError


def sheet(card: ContentCard) -> str:
    lines = [f"CONTENT REVIEW  {card.card_id}  v{card.version}  [{card.status.value}]",
             f"                {card.title}", ""]
    lines.append("  Content rules (value per reading):")
    for r in card.rules:
        differ = "   ⚑ the readings differ" if r.key in card.disagreements() else ""
        values = ", ".join(f"{reading}: {json.dumps(r.values[reading])}" for reading in card.readings)
        lines.append(f"    {r.key:<22} {values}{differ}")
        lines.append(f"      means: {r.meaning}")
        for s in r.sources:
            lines.append(f"      source{' p.' + str(s.page) if s.page else ''}: „{s.quote}“")
    lines.append("\n  Fields:")
    for f in card.fields:
        lines.append(f"    {f.name:<13} {f.format:<9} {'per period' if f.repeats else 'once':<10} {f.meaning}")
    n = card.placeholders()
    if n:
        lines.append(f"\n  ⚑ {n} source(s) are PLACEHOLDERS (not yet checked against the spec PDF)")
    lines += [f"  ? {q}" for q in card.open_questions]
    lines += [f"  ⚑ {f}" for f in card.flags]
    if card.status is not CardStatus.DRAFT:
        extra = f" accepting {card.approved_with_placeholders} placeholder source(s)" \
            if card.approved_with_placeholders else ""
        lines.append(f"\n  {card.status.value.upper()} by {card.reviewer} at {card.reviewed_at:%Y-%m-%d %H:%M}{extra}"
                     + (f": {card.review_note}" if card.review_note else ""))
    return "\n".join(lines)


def approve(card: ContentCard, reviewer: str, note: str = "", accept_placeholders: bool = False,
            now: datetime | None = None) -> ContentCard:
    if not reviewer.strip():
        raise ReviewError("approval needs the reviewer's name")
    if card.status is not CardStatus.DRAFT:
        raise ReviewError(f"only a draft can be approved; this content card is {card.status.value}")
    n = card.placeholders()
    if n and not accept_placeholders:
        raise ReviewError(f"{n} source(s) are still placeholders; approve explicitly accepting them, or check them "
                          f"against the spec PDF first")
    if n:
        note = (note + " | " if note else "") + f"approved accepting {n} placeholder source(s)"
    return ContentCard.model_validate(card.model_dump() | {
        "status": "approved", "reviewer": reviewer.strip(), "reviewed_at": now or datetime.now(), "review_note": note,
        "approved_hash": card.content_hash(), "approved_with_placeholders": n})


def reject(card: ContentCard, reviewer: str, reason: str, now: datetime | None = None) -> ContentCard:
    if not reviewer.strip() or not reason.strip():
        raise ReviewError("rejection needs the reviewer's name and a reason")
    if card.status is not CardStatus.DRAFT:
        raise ReviewError(f"only a draft can be rejected; this content card is {card.status.value}")
    return ContentCard.model_validate(card.model_dump() | {
        "status": "rejected", "reviewer": reviewer.strip(), "reviewed_at": now or datetime.now(),
        "review_note": reason.strip()})


def edit_rule(card: ContentCard, key: str, reading: str, value, reviewer: str) -> ContentCard:
    """Change one rule's value for one reading. Returns a new draft version (any approval is voided)."""
    if card.status is CardStatus.REJECTED:
        raise ReviewError("cannot edit a rejected content card; start again from the inventory")
    if reading not in card.readings:
        raise ReviewError(f"unknown reading {reading!r}; readings: {', '.join(card.readings)}")
    data = card.model_dump()
    rule = next((r for r in data["rules"] if r["key"] == key), None)
    if rule is None:
        raise ReviewError(f"no content rule {key!r}; rules: {', '.join(r.key for r in card.rules)}")
    before = rule["values"][reading]
    rule["values"][reading] = value
    data |= {"version": card.version + 1, "status": "draft", "reviewer": "", "reviewed_at": None, "review_note": "",
             "approved_hash": "", "approved_with_placeholders": 0,
             "flags": card.flags + [f"review: {reviewer} changed {key} for {reading}: {json.dumps(before)} -> "
                                    f"{json.dumps(value)}"]}
    try:
        return ContentCard.model_validate(data)
    except ValidationError as err:
        raise ReviewError(f"edit refused: {err.errors()[0]['msg'].removeprefix('Value error, ')}") from None


MENU = ("\n[a] approve   [r] reject   [e] edit a rule value   [s] show again   [q] save and quit   "
        "[!] quit WITHOUT saving")


def run(card: ContentCard, reviewer: str, ask: Callable[[str], str] | None = None,
        say: Callable[[str], None] | None = None) -> ContentCard | None:
    raw_ask = ask or input
    say = say or print

    def ask(prompt: str) -> str:  # noqa: F811 - drop the byte-order mark Windows PowerShell may pipe in
        return raw_ask(prompt).replace("﻿", "")

    say(sheet(card))
    while True:
        say(MENU)
        choice = ask("> ").strip().lower()
        try:
            if choice == "a":
                accept = False
                if card.placeholders():
                    say(f"{card.placeholders()} source(s) are still placeholders.")
                    accept = ask("Approve anyway, accepting them? Type YES: ").strip() == "YES"
                    if not accept:
                        continue
                card = approve(card, reviewer, ask("Note (optional): ").strip(), accept_placeholders=accept)
                say(f"Approved {card.card_id} v{card.version}.")
            elif choice == "r":
                card = reject(card, reviewer, ask("Reason: "))
                say(f"Rejected {card.card_id}.")
            elif choice == "e":
                key = ask("Rule (e.g. lookback_months): ").strip()
                reading = ask(f"Reading ({', '.join(card.readings)}): ").strip()
                value = json.loads(ask("New value (true, false, a number, null or \"newest_first\"): ").strip())
                card = edit_rule(card, key, reading, value, reviewer)
                say(sheet(card))
            elif choice == "s":
                say(sheet(card))
            elif choice == "q":
                return card
            elif choice == "!":
                return None
            else:
                say("Unknown choice.")
        except ReviewError as err:
            say(f"Not done: {err}")
        except json.JSONDecodeError:
            say("Not done: a value must be true, false, a number, null or text in double quotes.")
