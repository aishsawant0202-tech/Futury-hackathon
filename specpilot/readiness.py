"""Readiness check: is the testing pipeline ready for real use? Run it whenever new material arrives.

    python -m specpilot.readiness [--db specpilot.db]

Each check says READY or TO DO, and what to do. Checks that need the spec (hint words, content sources) run
automatically once the spec PDF has been loaded into the database (python -m specpilot.tools.pdf_ingest).
Exit code 0 = everything ready, 1 = something still to do.
"""
import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from specpilot import config, db


@dataclass
class Check:
    name: str
    ready: bool
    detail: str
    todo: str = ""

    def text(self) -> str:
        mark = "READY" if self.ready else "TO DO"
        return f"  [{mark}] {self.name}: {self.detail}" + ("" if self.ready else f"\n          -> {self.todo}")


def check_key(root: Path = config.ROOT) -> Check:
    env = root / ".env"
    if not env.exists():
        return Check("gateway key", False, ".env is missing",
                     "copy .env.example to .env and put the team key into LLM_API_KEY")
    values = dict(line.split("=", 1) for line in env.read_text(encoding="utf-8").splitlines()
                  if "=" in line and not line.lstrip().startswith("#"))
    key = values.get("LLM_API_KEY", "").strip()
    if not key or key == "put-the-team-key-here":
        return Check("gateway key", False, "LLM_API_KEY is empty or still the example value",
                     "put the team key into LLM_API_KEY in .env")
    return Check("gateway key", True, "LLM_API_KEY is set")


def check_pdf(pdf: Path = config.SPEC_PDF) -> Check:
    if pdf.exists():
        return Check("spec PDF", True, f"{pdf.name}")
    return Check("spec PDF", False, f"{pdf.name} is not in {pdf.parent}",
                 "copy the Verfahrensbeschreibung DTA EEL 13.4 PDF into data/specs/")


def _conn(path: Path) -> sqlite3.Connection | None:
    return db.connect(path) if Path(path).exists() else None


def check_spec_loaded(conn: sqlite3.Connection | None) -> Check:
    n = conn.execute("SELECT COUNT(*) FROM spec_pages").fetchone()[0] if conn else 0
    if n:
        return Check("spec loaded into the database", True, f"{n} pages")
    return Check("spec loaded into the database", False, "no spec pages in the database",
                 "python -m specpilot.tools.pdf_ingest  (after the PDF is in data/specs/)")


def check_rules(conn: sqlite3.Connection | None) -> Check:
    rules = db.list_requirements(conn) if conn else []
    example = [r for r in rules if r.quote.startswith("[BEISPIEL]")]
    if not rules:
        return Check("extracted rules", False, "no rules in the database",
                     "run the extractor (python -m specpilot.agents.extractor) or get the team's specpilot.db")
    if example:
        return Check("extracted rules", False, f"{len(example)} of {len(rules)} rules are EXAMPLE rules",
                     "use the real extraction, not the example database")
    return Check("extracted rules", True, f"{len(rules)} real rules")


def check_gold() -> Check:
    from specpilot.tools import gold
    ph = [rid for c in gold.load_all() for rid in gold.placeholder_ids(c)]
    if ph:
        return Check("gold card", False, f"{len(ph)} placeholder rule id(s) ({ph[0]} ...)",
                     "replace R-3.12-90x in data/gold/REQ-41-PRECONDITION.json with the real rule ids and quotes")
    return Check("gold card", True, "no placeholder rule ids")


def _spec_text(conn: sqlite3.Connection) -> str:
    from specpilot.tools import quote_check
    return quote_check.compact(" ".join(r[0] for r in conn.execute("SELECT text FROM spec_pages")))


def check_hint_words(conn: sqlite3.Connection | None) -> Check:
    from specpilot.tools import facts, quote_check
    if not conn or not conn.execute("SELECT COUNT(*) FROM spec_pages").fetchone()[0]:
        return Check("German hint words of the facts", False, "cannot be checked: the spec is not loaded",
                     "load the spec; this check then runs by itself")
    text = _spec_text(conn)
    missing = [f"{f.name}: '{t}'" for f in facts.FACTS.values() for t in f.german_terms
               if quote_check.compact(t) not in text]
    if missing:
        return Check("German hint words of the facts", False, f"{len(missing)} not found in the spec, e.g. "
                                                              f"{missing[0]}",
                     "correct or remove them in specpilot/tools/facts.py (they only guide the translator)")
    return Check("German hint words of the facts", True, "every hint word appears in the spec")


def check_content_sources(conn: sqlite3.Connection | None) -> Check:
    from specpilot import dbvo
    inv = dbvo.load()
    n = inv.placeholders()
    if n:
        extra = ""
        if conn and conn.execute("SELECT COUNT(*) FROM spec_pages").fetchone()[0]:
            extra = " (the spec is loaded: replace each placeholder with the real quote and page)"
        return Check("content card sources", False, f"{n} placeholder source(s), {len(inv.open_questions)} open "
                                                    f"question(s){extra}",
                     "check data/content/REQ-41-CONTENT.inventory.json against § 3.12 and set placeholder=false")
    return Check("content card sources", True, "no placeholders")


def check_cards(conn: sqlite3.Connection | None) -> Check:
    from specpilot import card_store
    if not conn:
        return Check("approved cards", False, "no team database", "store approved cards: python -m specpilot.card_store save")
    entries = card_store.entries(conn)
    approved = [e for e in entries if e.status == "approved"]
    stale = [e for e in approved if e.stale]
    if not approved:
        return Check("approved cards", False, "no approved card stored", "translate, review and store the cards")
    if stale:
        return Check("approved cards", False, f"{len(stale)} approved card(s) out of date ({stale[0].card_id} "
                                              f"v{stale[0].version})", "translate and review them again")
    return Check("approved cards", True, f"{len(approved)} approved version(s), all up to date")


def check_examples(root: Path = config.ROOT) -> Check:
    leftovers = [p for p in ("data/examples", "scripts/make_example_db.py", "tests/test_examples.py")
                 if (root / p).exists()]
    if leftovers:
        return Check("example data removed", False, f"still present: {', '.join(leftovers)}",
                     "delete the files listed in data/examples/README.md once the real data works")
    return Check("example data removed", True, "no example data left")


def run(db_path: Path = config.DB_PATH) -> list[Check]:
    conn = _conn(Path(db_path))
    try:
        return [check_key(), check_pdf(), check_spec_loaded(conn), check_rules(conn), check_gold(),
                check_hint_words(conn), check_content_sources(conn), check_cards(conn), check_examples()]
    finally:
        if conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Is the testing pipeline ready for real use?")
    p.add_argument("--db", default=str(config.DB_PATH))
    a = p.parse_args(argv)
    checks = run(Path(a.db))
    todo = [c for c in checks if not c.ready]
    print(f"READINESS: {len(checks) - len(todo)} of {len(checks)} ready")
    print("\n".join(c.text() for c in checks))
    return 1 if todo else 0


if __name__ == "__main__":
    raise SystemExit(main())
