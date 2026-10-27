"""DBVO calculator (Station 6 level 2): what the reason 41 DBVO block must contain, per dummy person and reading.

Inputs: an APPROVED content card (the rule values per reading), the APPROVED precondition card (does the message
have to be sent at all?) and a dummy file (the SQL file of Station 5). The precondition card must be the dummy
file's own card or its background card. Everything is computed from each person's RAW values; the design labels
are not used.

Per person and reading:
  1. message?    the precondition card's conditions on the raw values (content card rule only_with_message)
  2. candidates  earlier absences that ended before the current one; without a sick note they are skipped when
                 certified_only says so (listed, with the reason)
  3. the chain   walk back from the current absence, newest first: take an illness while the free time between it
                 and the next one is within gap_months; EXACTLY gap_months continues the chain only if
                 gap_exactly_counts; with lookback_months, an illness ending before that window stops the chain
  4. fields      ABGABEGRUND, AU-AB-AG (current start), ANZAHL-AU (count, 2 digits), BEGINN-AU-n / ENDE-AU-n (YYYYMMDD),
                 in the order the content card says
Earlier absences that overlap or touch are refused: the spec text at hand does not say how to report them (open
question). Nothing here uses the team's reference model.
"""
import json
from pathlib import Path

from specpilot.criteria import CriteriaCard
from specpilot.dbvo import ContentCard, ContentResult, ReportedPeriod
from specpilot.models import AbsencePeriod, Scenario
from specpilot.tools import facts
from specpilot.tools.date_helpers import gap_vs_months, window_before
from specpilot.tools.self_check import Problem, check_card

KNOWN_FIELDS = {"ABGABEGRUND", "AU-AB-AG", "ANZAHL-AU", "BEGINN-AU-n", "ENDE-AU-n"}


class ContentError(Exception):
    """The content cannot be computed; the message says why."""


def check_cards(content: ContentCard, precondition: CriteriaCard) -> None:
    if not content.approval_valid:
        raise ContentError(f"content card {content.card_id} v{content.version} is not approved (or changed since)")
    problems = check_card(precondition)
    if problems:
        raise ContentError(f"precondition card: {'; '.join(p.message for p in problems)}")
    if content.reason_code not in precondition.reason_codes:
        raise ContentError(f"content card is for reason {content.reason_code}, the precondition card for "
                           f"{precondition.reason_codes}")
    if precondition.readings():
        raise ContentError(f"precondition card {precondition.id} still has open readings; resolve them first")
    unknown = sorted({f.name for f in content.fields} - KNOWN_FIELDS)
    if unknown:
        raise ContentError(f"the content card lists field(s) {unknown} that no rule here can fill yet")


def must_send(precondition: CriteriaCard, person: Scenario) -> bool:
    values = facts.evaluate(person)
    met = [facts.holds(values[c.fact], c.op, c.value) for c in precondition.criteria]
    return all(met) if precondition.logic == "AND" else any(met)


def _no_overlaps(earlier: list[AbsencePeriod]) -> None:
    ordered = sorted(earlier, key=lambda p: p.start)
    for a, b in zip(ordered, ordered[1:]):
        if b.start.toordinal() <= a.end.toordinal() + 1:
            raise ContentError(f"earlier absences {a.start:%d.%m.%Y}-{a.end:%d.%m.%Y} and {b.start:%d.%m.%Y}-"
                               f"{b.end:%d.%m.%Y} overlap or touch: how to report them is an open question")


def content_for(content: ContentCard, reading: str, person: Scenario, send: bool, person_id: str,
                neutral_id: str) -> ContentResult:
    if content.rule("only_with_message", reading) and not send:
        return ContentResult(person_id=person_id, neutral_id=neutral_id, reading=reading, message_expected=False,
                             stop_reason="the message must not be sent")
    gap, exactly = content.rule("gap_months", reading), content.rule("gap_exactly_counts", reading)
    lookback, certified_only = content.rule("lookback_months", reading), content.rule("certified_only", reading)
    current = person.current.start
    earlier = sorted((p for p in person.previous if p.end < current), key=lambda p: p.end, reverse=True)
    _no_overlaps(earlier)

    skipped, candidates = [], []
    for p in earlier:
        if certified_only and not p.certified:
            skipped.append(f"{p.start:%d.%m.%Y}-{p.end:%d.%m.%Y}: no sick note, not a previous illness (certified_only)")
        else:
            candidates.append(p)

    limit = window_before(current, lookback).start if lookback else None
    chain: list[tuple[AbsencePeriod, str]] = []
    anchor, stop = current, "no further earlier illness"
    for p in candidates:
        free = (anchor - p.end).days - 1
        g = gap_vs_months(p.end, anchor, gap)
        span = f"{p.start:%d.%m.%Y}-{p.end:%d.%m.%Y}"
        if g > 0:
            stop = f"{span}: free time of {free} days before {anchor:%d.%m.%Y} is more than {gap} months"
            break
        if g == 0 and not exactly:
            stop = f"{span}: free time before {anchor:%d.%m.%Y} is exactly {gap} months, which ends the chain under {reading}"
            break
        if limit and p.end < limit:
            stop = f"{span}: ends before the {lookback}-month limit ({limit:%d.%m.%Y}) of {reading}"
            break
        why = (f"free time of {free} days before {anchor:%d.%m.%Y}: exactly {gap} months, which continues the chain "
               f"under {reading}" if g == 0 else f"free time of {free} days before {anchor:%d.%m.%Y}: within {gap} months")
        chain.append((p, why))
        anchor = p.start
    if content.rule("order", reading) == "oldest_first":
        chain.reverse()

    periods = [ReportedPeriod(n=n, start=p.start, end=p.end, why=why) for n, (p, why) in enumerate(chain, start=1)]
    names = {f.name for f in content.fields}
    fields: dict[str, str] = {}
    if "ABGABEGRUND" in names:
        fields["ABGABEGRUND"] = content.reason_code
    if "AU-AB-AG" in names:
        fields["AU-AB-AG"] = f"{current:%Y%m%d}"
    if "ANZAHL-AU" in names:
        fields["ANZAHL-AU"] = f"{len(periods):02d}"
    for p in periods:
        if "BEGINN-AU-n" in names:
            fields[f"BEGINN-AU-{p.n}"] = f"{p.start:%Y%m%d}"
        if "ENDE-AU-n" in names:
            fields[f"ENDE-AU-{p.n}"] = f"{p.end:%Y%m%d}"
    return ContentResult(person_id=person_id, neutral_id=neutral_id, reading=reading, message_expected=True,
                         fields=fields, periods=periods, skipped=skipped, stop_reason=stop)


def compute_content(content: ContentCard, precondition: CriteriaCard, dummy_path: Path) -> list[ContentResult]:
    """The DBVO content of every dummy person, for every reading of the content card."""
    from specpilot.tools.expected_results import load_dummy
    check_cards(content, precondition)
    d = load_dummy(Path(dummy_path))
    own = (d.card_id, d.card_hash) == (precondition.id, precondition.content_hash())
    background = (d.background_card_id, d.background_card_hash) == (precondition.id, precondition.content_hash())
    if not (own or background):
        raise ContentError(f"{precondition.id} is neither the card nor the background card of {Path(dummy_path).name} "
                           f"(fingerprints differ)")
    results = []
    for p in d.people:
        person = d.scenario_of(p)
        send = must_send(precondition, person)
        results += [content_for(content, reading, person, send, p.person_id, p.neutral_id)
                    for reading in content.readings]
    return results


def content_file(content: ContentCard, precondition: CriteriaCard, dummy_path: Path):
    """compute_content wrapped in the checked ContentFile form, tied to the exact dummy file."""
    from specpilot.dbvo import ContentFile
    from specpilot.tools.expected_results import load_dummy, sha256
    results = compute_content(content, precondition, dummy_path)
    d = load_dummy(Path(dummy_path))
    return ContentFile(content_card_id=content.card_id, content_card_version=content.version,
                       content_card_hash=content.content_hash(),
                       approved_with_placeholders=content.approved_with_placeholders,
                       precondition_card_id=precondition.id, precondition_card_hash=precondition.content_hash(),
                       dummy_file=Path(dummy_path).name, dummy_sha256=sha256(Path(dummy_path)), seed=d.seed,
                       readings=content.readings, person_ids=[p.person_id for p in d.people], results=results)


# --- checks: every result obeys the card's rules; the readings differ only for a reason --------------------------

def check_result(content: ContentCard, person: Scenario, r: ContentResult, send: bool) -> list[Problem]:
    """Re-check one result against the card's rules, independently of how the chain was walked."""
    out: list[Problem] = []

    def bad(check: str, message: str) -> None:
        out.append(Problem(f"{r.person_id} {r.reading}", check, message))

    if r.message_expected != (send or not content.rule("only_with_message", r.reading)):
        bad("message", f"message_expected={r.message_expected}, but the precondition card says "
                       f"{'send' if send else 'do not send'}")
    if not r.message_expected:
        return out
    current = person.current.start
    gap, exactly = content.rule("gap_months", r.reading), content.rule("gap_exactly_counts", r.reading)
    lookback, certified_only = content.rule("lookback_months", r.reading), content.rule("certified_only", r.reading)
    limit = window_before(current, lookback).start if lookback else None
    earlier = [p for p in person.previous if p.end < current]
    own = {(p.start, p.end): p for p in earlier}

    newest_first = sorted(r.periods, key=lambda p: p.end, reverse=True)
    expected_order = newest_first if content.rule("order", r.reading) == "newest_first" else newest_first[::-1]
    if [p.n for p in expected_order] != [p.n for p in r.periods]:
        bad("order", f"periods are not in the order {content.rule('order', r.reading)}")
    anchor = current
    for p in newest_first:
        span = f"{p.start:%d.%m.%Y}-{p.end:%d.%m.%Y}"
        src = own.get((p.start, p.end))
        if src is None:
            bad("period", f"{span} is not one of the person's earlier absences")
            continue
        if certified_only and not src.certified:
            bad("period", f"{span} has no sick note but was reported")
        g = gap_vs_months(p.end, anchor, gap)
        if g > 0 or (g == 0 and not exactly):
            bad("chain", f"{span}: the free time before {anchor:%d.%m.%Y} breaks the chain under {r.reading}, "
                         f"but the period was reported")
        if limit and p.end < limit:
            bad("lookback", f"{span} ends before the {lookback}-month limit")
        anchor = p.start
    # the chain must not stop too early: the next older candidate must break a rule
    older = sorted((p for p in earlier if p.end < anchor and (p.certified or not certified_only)),
                   key=lambda p: p.end, reverse=True)
    if older:
        nxt = older[0]
        g = gap_vs_months(nxt.end, anchor, gap)
        if not (g > 0 or (g == 0 and not exactly) or (limit and nxt.end < limit)):
            bad("chain", f"stopped too early: {nxt.start:%d.%m.%Y}-{nxt.end:%d.%m.%Y} continues the chain under "
                         f"{r.reading} but was not reported")
    if certified_only:
        uncertified = sorted(f"{p.start:%d.%m.%Y}-{p.end:%d.%m.%Y}" for p in earlier if not p.certified)
        if sorted(s.split(":")[0] for s in r.skipped) != uncertified:
            bad("skipped", f"skipped {r.skipped}, but the absences without a sick note are {uncertified}")
    return out


def check_content(content: ContentCard, precondition: CriteriaCard, dummy_path: Path, cf):
    """All checks for a content file: each result against the rules, the readings against each other, and the
    message against the design. Returns a Consistency report (problems block writing)."""
    from specpilot.tools.expected_results import Consistency, load_dummy
    d = load_dummy(Path(dummy_path))
    out = Consistency()
    by = {(r.person_id, r.reading): r for r in cf.results}
    has_background = (d.background_card_id, d.background_card_hash) == (precondition.id, precondition.content_hash())
    edges = {"an exact 6-month link": 0, "the 12-month limit": 0}
    for p in d.people:
        person = d.scenario_of(p)
        send = must_send(precondition, person)
        if has_background and not send:
            out.problems.append(Problem(p.person_id, "message", "every person of a content-scenarios file must send "
                                                                "the message (background), but this one must not"))
        if not has_background and send != bool(p.all_true):
            out.problems.append(Problem(p.person_id, "message",
                                        f"must{'' if send else ' not'} send, but its design label all_true = {p.all_true}"))
        results = [by[(p.person_id, reading)] for reading in cf.readings]
        for r in results:
            out.problems += check_result(content, person, r, send)
            out.compared += 1
        values = facts.evaluate(person)
        if len({json.dumps(r.fields, sort_keys=True) for r in results}) > 1:
            if not (values["chain_link_exactly_6m"] or values["prior_ends_before_12m"]):
                out.problems.append(Problem(p.person_id, "readings", "the readings give different content, but the "
                                                                     "person has neither an exact 6-month link nor an "
                                                                     "illness before the 12-month window"))
            stops = " ".join(r.stop_reason for r in results)
            edges["an exact 6-month link" if "exactly" in stops else "the 12-month limit"] += 1
    out.notes.append("the readings give different content through " +
                     " and ".join(f"{k} for {v} people" for k, v in edges.items()))
    return out
