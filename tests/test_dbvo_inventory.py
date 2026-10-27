"""DBVO content inventory: the field and rule list loads, is complete per reading, and refuses broken lists."""
import json

import pytest
from pydantic import ValidationError

from specpilot import dbvo
from specpilot.dbvo import Inventory, load

INV = load()


def data() -> dict:
    return json.loads((dbvo.INVENTORY_DIR / "REQ-41-CONTENT.inventory.json").read_text(encoding="utf-8"))


def rule(d: dict, key: str) -> dict:
    return next(r for r in d["rules"] if r["key"] == key)


# --- the reason 41 inventory ------------------------------------------------------------------------------------

def test_fields():
    assert [f.name for f in INV.fields] == ["ABGABEGRUND", "AU-AB-AG", "ANZAHL-AU", "BEGINN-AU-n", "ENDE-AU-n"]
    assert {f.name: f.format for f in INV.fields}["BEGINN-AU-n"] == "jjjjmmtt"
    assert [f.name for f in INV.fields if f.repeats] == ["BEGINN-AU-n", "ENDE-AU-n"]


def test_rule_values_per_reading():
    assert [INV.rule("gap_months", r) for r in INV.readings] == [6, 6]
    assert [INV.rule("gap_exactly_counts", r) for r in INV.readings] == [True, False]
    assert [INV.rule("lookback_months", r) for r in INV.readings] == [None, 12]
    assert INV.rule("certified_only", "p.135") is True and INV.rule("order", "p.136") == "newest_first"


def test_the_two_contradictions_are_flagged():
    assert INV.disagreements() == ["gap_exactly_counts", "lookback_months"]
    assert "⚑ readings differ" in INV.describe()


def test_every_source_is_still_a_placeholder():
    sources = [s for x in [*INV.fields, *INV.rules] for s in x.sources]
    assert all(s.placeholder and "PLACEHOLDER" in s.quote for s in sources)
    assert INV.placeholders() == len(sources) == 14
    assert "14 source(s) still PLACEHOLDER" in INV.describe()


def test_open_questions_are_shown():
    assert len(INV.open_questions) == 4 and all(f"? {q}" in INV.describe() for q in INV.open_questions)


def test_unknown_reading():
    with pytest.raises(KeyError, match="unknown reading"):
        INV.rule("gap_months", "p.999")


def test_does_not_use_the_teammates_reference_model():
    from pathlib import Path
    assert "reference_model" not in Path("specpilot/dbvo.py").read_text(encoding="utf-8")


# --- broken lists are refused ---------------------------------------------------------------------------------------

def refused(change, message):
    d = data()
    change(d)
    with pytest.raises(ValidationError, match=message):
        Inventory.model_validate(d)


def test_rule_needs_a_value_for_every_reading():
    refused(lambda d: rule(d, "lookback_months")["values"].pop("p.136"), "one value per reading")


@pytest.mark.parametrize("key, value, message", [
    ("gap_months", 0, "not allowed"),
    ("gap_months", "6", "not allowed"),
    ("gap_exactly_counts", "yes", "not allowed"),
    ("lookback_months", -12, "not allowed"),
    ("order", "random", "not allowed"),
])
def test_rule_values_must_fit(key, value, message):
    refused(lambda d: rule(d, key)["values"].update({"p.135": value}), message)


def test_unknown_or_missing_rules():
    refused(lambda d: d["rules"].append({"key": "colour", "meaning": "test rule", "values": {"p.135": 1, "p.136": 1},
                                         "sources": [{"quote": "Testzitat"}]}), "unknown content rule")
    refused(lambda d: d["rules"].pop(0), "missing")
    refused(lambda d: d["rules"].append(d["rules"][0]), "listed twice")


def test_field_problems():
    refused(lambda d: d["fields"].append(d["fields"][0]), "field is listed twice")
    refused(lambda d: d["fields"].pop(4), "BEGINN-AU-n / ENDE-AU-n pair")  # BEGINN without ENDE
    refused(lambda d: d["fields"].pop(2), "need ANZAHL-AU")
    refused(lambda d: d["fields"][1].update(repeats=True), "ends in -n")
    refused(lambda d: d["fields"][1].update(format="dd.mm.yyyy"), "format")


def test_other_problems():
    refused(lambda d: d.update(reason_code="40"), "unknown reason code")
    refused(lambda d: d.update(readings=["p.135", "p.135"]), "listed twice")
    refused(lambda d: d["fields"][0].update(sources=[]), "at least 1")
    refused(lambda d: d.update(extra=1), "Extra inputs")
