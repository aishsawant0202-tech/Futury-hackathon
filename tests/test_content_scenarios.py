"""Content-edge dummies: background conditions, start-date retries, the scenarios card, and the whole chain."""
import datetime as dt

import pytest
from pydantic import ValidationError

from specpilot.branches import BranchSet, Target
from specpilot.criteria import CriteriaCard
from specpilot.dummies import build as build_dummies
from specpilot.review import approve
from specpilot.tools import facts, scenarios, value_finder
from specpilot.tools.branch_maker import BranchError, make_branches
from specpilot.tools.scenarios import ScenarioError
from specpilot.tools.self_check import check
from specpilot.tools.value_finder import Found, check_person, make_people, solve_varied
from tests.test_review import RULES, draft


@pytest.fixture(scope="module")
def precondition():
    return approve(draft(), "Examiner A", RULES)


@pytest.fixture(scope="module")
def scenarios_card(precondition):
    return approve(scenarios.build(precondition, RULES), "Examiner A", RULES)


@pytest.fixture(scope="module")
def branches(scenarios_card, precondition):
    return make_branches(scenarios_card, background=precondition)


@pytest.fixture(scope="module")
def people(branches):
    return make_people(branches, 4711)


@pytest.fixture(scope="module")
def dummy(people, branches):
    return build_dummies(people, branches, "test")


# --- the scenarios card ---------------------------------------------------------------------------------------

def test_scenarios_card_from_the_precondition_cards_unused_rules(scenarios_card, precondition):
    assert scenarios_card.id == "REQ-41-CONTENT-SCENARIOS" and scenarios_card.kind.value == "content"
    assert [c.fact for c in scenarios_card.criteria] == ["chain_link_exactly_6m", "prior_ends_before_12m",
                                                         "uncertified_inside", "prior_gaps_under_180d"]
    assert scenarios_card.source_rule_ids == [u.rule_id for u in precondition.unused_rules]
    by = {c.fact: c.source_rule_ids for c in scenarios_card.criteria}
    assert by["chain_link_exactly_6m"] == ["R-3.12-006"] and by["prior_ends_before_12m"] == ["R-3.12-007"]
    assert [u.rule_id for u in scenarios_card.unused_rules] == ["R-3.12-005"]
    assert any(f.startswith("TEST DESIGN") for f in scenarios_card.flags)


def test_quotes_are_the_rules_exact_words(scenarios_card):
    rules = {r.id: r for r in RULES}
    for c in scenarios_card.criteria:
        assert c.quote == rules[c.source_rule_ids[0]].quote


def test_builder_refuses_without_the_content_rules(precondition):
    with pytest.raises(ScenarioError, match="not in the database"):
        scenarios.build(precondition, RULES[:5])
    no_twelve = [r.model_copy(update={"quote": "Zurückzugehen ist jedoch maximal innerhalb der letzten Zeit."})
                 if r.id == "R-3.12-007" else r for r in RULES]
    with pytest.raises(ScenarioError, match="12-month limit"):
        scenarios.build(precondition, no_twelve)


# --- branches with background conditions --------------------------------------------------------------------------

def test_16_branches_each_with_the_send_conditions_as_background(branches, precondition):
    assert len(branches.branches) == 16
    assert (branches.background_card_id, branches.background_card_hash) == (precondition.id, precondition.content_hash())
    for b in branches.branches:
        assert [(t.fact, t.op, t.value) for t in b.background] == [(c.fact, c.op, c.value) for c in precondition.criteria]
        assert all(t.target is Target.MET for t in b.background)
        assert not {t.fact for t in b.background} & set(b.free_facts)
    assert "every person also meets 5 condition(s) of REQ-41-PRECONDITION" in branches.summary()


def test_background_rules_in_the_branch_form(branches):
    data = branches.model_dump(mode="json")
    data["branches"][3]["background"][1]["target"] = "failed"
    with pytest.raises(ValidationError, match="every background condition must be met"):
        BranchSet.model_validate(data)
    data = branches.model_dump(mode="json")
    data["branches"][3]["background"] = data["branches"][3]["background"][:-1]
    with pytest.raises(ValidationError, match="same background"):
        BranchSet.model_validate(data)
    data = branches.model_dump(mode="json") | {"background_card_hash": ""}
    with pytest.raises(ValidationError, match="go together"):
        BranchSet.model_validate(data)


def test_card_and_background_may_not_share_a_fact(precondition):
    with pytest.raises(BranchError, match="constrain the same fact"):
        make_branches(precondition, background=precondition)


def test_background_with_open_readings_is_refused(scenarios_card):
    from tests.test_branch_maker import alt, card as sized
    c = sized(2, alternatives={2: alt("prior_certified_6m", "!=", False, "negated")})
    with pytest.raises(BranchError, match="open readings"):
        make_branches(scenarios_card, background=c)


# --- people ------------------------------------------------------------------------------------------------------------

def test_12_people_and_4_proven_impossible(people):
    assert (len(people.people), len(people.impossible), len(people.unsolved)) == (12, 4, 0)


def test_the_impossible_ones_ask_for_an_exact_link_and_gaps_under_180_days(people, branches):
    by = {b.branch_id: b for b in branches.branches}
    for x in people.impossible:
        targets = {t.fact: t.target for t in by[x.branch_id].targets}
        assert targets["chain_link_exactly_6m"] is Target.MET and targets["prior_gaps_under_180d"] is Target.MET
        assert "proved for each of 8 start dates" in x.reason


def test_every_person_meets_the_background_and_its_branch(people, branches):
    by = {b.branch_id: b for b in branches.branches}
    for p in people.people:
        assert check_person(by[p.branch_id], p.scenario) == []
        assert facts.evaluate(p.scenario)["prior_certified_6m"]  # a send condition, from the background


def test_four_people_sit_on_the_exact_6_month_link(people):
    exact = [p for p in people.people if facts.evaluate(p.scenario)["chain_link_exactly_6m"]]
    assert len(exact) == 4


def test_other_start_dates_are_tried_before_impossible(branches, monkeypatch):
    """First start date proven impossible, second works: the person is found."""
    calls = []

    def fake(branch, w, timeout_ms):
        calls.append(w.start)
        if len(calls) == 1:
            return Found(branch.branch_id, "impossible", reason="for this start date")
        return value_finder.solve_branch(branch, w.start, timeout_ms)

    monkeypatch.setattr(value_finder, "_try_start", fake)
    r = solve_varied(branches.branches[1], 4711)
    assert r.outcome == "found" and len(calls) == 2 and calls[0] != calls[1]


def test_timeout_does_not_trigger_other_start_dates(branches, monkeypatch):
    calls = []
    monkeypatch.setattr(value_finder, "_try_start",
                        lambda b, w, t: calls.append(w.start) or Found(b.branch_id, "unsolved", reason="timeout"))
    assert solve_varied(branches.branches[1], 4711).outcome == "unsolved" and len(calls) == 1


def test_other_start_dates_are_reproducible():
    a = value_finder.other_starts(4711, "X-B01", dt.date(2027, 3, 14))
    assert a == value_finder.other_starts(4711, "X-B01", dt.date(2027, 3, 14)) and len(a) >= 6
    assert dt.date(2027, 3, 14) not in a


# --- self-check with a background ----------------------------------------------------------------------------------------

def test_self_check_passes_with_the_background_card(scenarios_card, branches, dummy, precondition):
    r = check(scenarios_card, branches, dummy, precondition)
    assert r.ok and r.people_checked == 12 and len(r.notes) == 4


def test_self_check_needs_the_right_background_card(scenarios_card, branches, dummy, precondition):
    assert any(p.check == "background" for p in check(scenarios_card, branches, dummy).problems)
    other = approve(CriteriaCard.model_validate(precondition.model_dump() | {
        "version": 2, "status": "draft", "reviewer": "", "reviewed_at": None, "approved_hash": ""}), "A", RULES)
    r = check(scenarios_card, branches, dummy, other)
    assert any("fingerprints differ" in p.message for p in r.problems)


def test_self_check_verifies_the_background_from_the_raw_values(scenarios_card, branches, dummy, precondition):
    people = list(dummy.people)
    people[0] = people[0].model_copy(update={"statutory_insured": False})
    r = check(scenarios_card, branches, dummy.model_copy(update={"people": people}), precondition)
    assert any(p.check == "background" and "statutory_insured" in p.message for p in r.problems)


def test_dummy_file_records_its_background(dummy, precondition):
    assert (dummy.background_card_id, dummy.background_card_hash) == (precondition.id, precondition.content_hash())


# --- the dummy writer command --------------------------------------------------------------------------------------------

def test_writer_command_needs_the_background_card(scenarios_card, branches, precondition, tmp_path, capsys):
    from specpilot.review import save_card
    from specpilot.tools import dummy_writer
    from specpilot.tools.branch_maker import write_branches
    save_card(scenarios_card, tmp_path / "s.json")
    save_card(precondition, tmp_path / "p.json")
    bfile, _ = write_branches(branches, tmp_path)
    assert dummy_writer.main([str(tmp_path / "s.json"), str(bfile), "--seed", "1", "--dry-run"]) == 1
    assert "pass that exact approved card with --background" in capsys.readouterr().err
    assert dummy_writer.main([str(tmp_path / "s.json"), str(bfile), "--seed", "4711", "--out", str(tmp_path / "d"),
                              "--background", str(tmp_path / "p.json")]) == 0
    from specpilot.tools.dummy_writer import load
    d = load(tmp_path / "d" / "REQ-41-CONTENT-SCENARIOS-v1-seed4711.sqlite")
    assert d.background_card_id == precondition.id and len(d.people) == 12
