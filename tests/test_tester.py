"""Testing agent tests with a fake model: no gateway, no key, same result every time."""
import pytest

from specpilot import db, llm
from specpilot.agents import tester
from specpilot.agents.tester import CaseDraft, CaseDrafts, TesterError
from specpilot.models import Actor, CaseKind, Requirement, RuleKind

DOC = "Test-Spezifikation"


def _rule(n, section="3.12", actor=Actor.EMPLOYER, verified=True):
    return Requirement(
        id=f"R-{section}-{n:03d}", section=section, page=135, quote=f"eine deutsche Regel Nummer {n}",
        statement=f"rule {n}", kind=RuleKind.PRECONDITION, actor=actor, quote_verified=verified, document=DOC,
    )


def _case(*ids, kind=CaseKind.POSITIVE):
    return CaseDraft(requirement_ids=list(ids), kind=kind, title=f"case {ids}", given="given", steps=["step"], expected="expected")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.save_sections(c, DOC, [
        {"number": "3.12", "title": "DBVO", "first_page": 135, "last_page": 136},
        {"number": "3.13", "title": "DBHE", "first_page": 137, "last_page": 137},
    ])
    for r in (_rule(1), _rule(2), _rule(3, actor=Actor.INSURER), _rule(4, verified=False), _rule(1, "3.13")):
        db.save_requirement(c, r)
    yield c
    c.close()


def test_topics_count_testable_rules(conn):
    topics = tester.list_topics(conn, DOC)
    assert [(t["number"], t["title"], t["rules"], t["all_rules"]) for t in topics] == [("3.12", "DBVO", 2, 4), ("3.13", "DBHE", 1, 1)]
    assert [r.id for r in tester.topic_rules(conn, "3.12")] == ["R-3.12-001", "R-3.12-002"]


def test_generate_numbers_saves_and_keeps_only_topic_rules(conn, monkeypatch):
    prompts = []

    def fake(model, system, user, schema):
        prompts.append(user)
        assert schema is CaseDrafts
        return CaseDrafts(cases=[_case("R-3.12-001"), _case("R-3.12-002", "R-3.12-003", kind=CaseKind.NEGATIVE), _case("R-9.9-001")])

    monkeypatch.setattr(llm, "ask_json", fake)
    res = tester.generate(conn, "3.12", cases=2, document=DOC)

    assert "R-3.12-003" not in prompts[0] and "R-3.12-004" not in prompts[0]  # insurer and unverified rules are not given
    assert len(prompts) == 2 and "not rules of this topic" in prompts[1]  # the bad citations went back to the model
    assert [c.id for c in res.cases] == ["TC-3.12-001", "TC-3.12-002"]
    assert res.cases[1].requirement_ids == ["R-3.12-002"]
    assert res.dropped == 1 and res.uncovered == []
    assert [c.id for c in db.list_topic_cases(conn, "3.12")] == ["TC-3.12-001", "TC-3.12-002"]
    assert tester.list_topics(conn, DOC)[0]["cases"] == 2


def test_uncovered_rule_is_reported(conn, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", lambda m, s, u, schema: CaseDrafts(cases=[_case("R-3.12-001")]))
    res = tester.generate(conn, "3.12", cases=1, document=DOC)
    assert res.uncovered == ["R-3.12-002"]
    assert "R-3.12-002" in db.list_audit(conn, "3.12")[-1].detail


def test_new_run_replaces_old_cases(conn, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", lambda m, s, u, schema: CaseDrafts(cases=[_case("R-3.12-001", "R-3.12-002")] * 3))
    tester.generate(conn, "3.12", cases=3, document=DOC)
    tester.generate(conn, "3.12", cases=1, document=DOC)
    assert len(db.list_topic_cases(conn, "3.12")) == 1


def test_run_limits(conn):
    with pytest.raises(TesterError, match="at least one"):
        tester.run(conn, [], document=DOC)
    with pytest.raises(TesterError, match="at most 15"):
        tester.run(conn, [f"3.{n}" for n in range(16)], document=DOC)
    with pytest.raises(TesterError, match="9.9"):
        tester.run(conn, ["9.9"], document=DOC)
    db.save_requirement(conn, _rule(1, "4.1", verified=False))
    with pytest.raises(TesterError, match="no testable rules"):
        tester.generate(conn, "4.1", document=DOC)


def test_export_keeps_expected_answers_out_of_the_test_case_file(conn, monkeypatch, tmp_path):
    case = _case("R-3.12-001", "R-3.12-002").model_copy(update={"title": "sick note present", "expected": "message is sent"})
    monkeypatch.setattr(llm, "ask_json", lambda m, s, u, schema: CaseDrafts(cases=[case]))
    tester.generate(conn, "3.12", cases=1, document=DOC)
    cases_file, expected_file, cases_pdf, expected_pdf = tester.export(conn, ["3.12"], tmp_path / "out")
    assert cases_pdf.read_bytes().startswith(b"%PDF") and expected_pdf.read_bytes().startswith(b"%PDF")
    vendor = cases_file.read_text(encoding="utf-8-sig")
    assert vendor.splitlines()[0] == "id,topic,title,given,steps"
    assert "R-3.12" not in vendor and "message is sent" not in vendor
    expected = expected_file.read_text(encoding="utf-8-sig")
    assert "message is sent" in expected
    assert "R-3.12-001 (p.135), R-3.12-002 (p.135)" in expected
