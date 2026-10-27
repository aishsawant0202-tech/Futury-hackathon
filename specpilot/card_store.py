"""Card store (Station 2, step 8): every card version in the team database, and out-of-date cards caught.

Table criteria_cards in the team database (specpilot.db), one row per card version:
    criteria cards (REQ-41-PRECONDITION, REQ-41-CONTENT-SCENARIOS, ...) and content cards (REQ-41-CONTENT)
    status, content fingerprint, and for criteria cards the fingerprint of every extracted rule the card cites

Rules:
- a stored version is permanent: storing the same id and version with DIFFERENT content is refused (edit in
  review, which makes a new version). The same content may be stored again, e.g. once approved.
- a card is OUT OF DATE when one of its rules changed or disappeared in the database since the card was stored
  (the extractor was run again). The report names the rules; out-of-date cards are not exported, and the
  branch maker refuses them (--db).
- export writes, per card, the newest APPROVED and up-to-date version as a JSON file the other commands read.

Run:
    python -m specpilot.card_store save card.json --db specpilot.db
    python -m specpilot.card_store list --db specpilot.db
    python -m specpilot.card_store check --db specpilot.db          (exit 1 if an approved card is out of date)
    python -m specpilot.card_store export --db specpilot.db [--out output/cards]
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from specpilot import config, db
from specpilot.criteria import CardStatus, CriteriaCard
from specpilot.dbvo import ContentCard
from specpilot.models import Requirement

EXPORT_DIR = config.ROOT / "output" / "cards"

SCHEMA = """
CREATE TABLE IF NOT EXISTS criteria_cards (
    card_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('criteria', 'content')),
    status       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rule_hashes  TEXT NOT NULL,
    data         TEXT NOT NULL,
    PRIMARY KEY (card_id, version)
);
"""

Card = CriteriaCard | ContentCard


class StoreError(Exception):
    """The store refused; the message says why."""


def _conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    return conn


def card_id(card: Card) -> str:
    return card.id if isinstance(card, CriteriaCard) else card.card_id


def rule_fingerprint(r: Requirement) -> str:
    """What a card relies on in a rule: its words and labels (not the extractor's votes or flags)."""
    content = r.model_dump(mode="json", include={"id", "page", "quote", "statement", "kind", "actor",
                                                 "reason_codes", "document"})
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _rule_hashes(conn: sqlite3.Connection, card: Card) -> dict[str, str]:
    if not isinstance(card, CriteriaCard):
        return {}  # a content card cites the spec directly, not extracted rules
    out = {}
    for rid in card.source_rule_ids:
        r = db.get_requirement(conn, rid)
        if r is None:
            raise StoreError(f"{card.id} cites {rid}, which is not in the database")
        out[rid] = rule_fingerprint(r)
    return out


def save(conn: sqlite3.Connection, card: Card) -> str:
    """Store one card version. Returns 'stored', 'updated' (same content, new status) or 'unchanged'."""
    conn = _conn(conn)
    cid, kind = card_id(card), "criteria" if isinstance(card, CriteriaCard) else "content"
    old = conn.execute("SELECT content_hash, data FROM criteria_cards WHERE card_id = ? AND version = ?",
                       (cid, card.version)).fetchone()
    data = card.model_dump_json()
    if old and old[0] != card.content_hash():
        raise StoreError(f"{cid} v{card.version} is already stored with different content; versions are permanent "
                         f"(edit it in review, which makes a new version)")
    if old and old[1] == data:
        return "unchanged"
    hashes = json.dumps(_rule_hashes(conn, card), sort_keys=True)
    with conn:
        conn.execute("INSERT OR REPLACE INTO criteria_cards VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (cid, card.version, kind, card.status.value, card.content_hash(), hashes, data))
    return "updated" if old else "stored"


def load(conn: sqlite3.Connection, cid: str, version: int) -> Card:
    row = _conn(conn).execute("SELECT kind, data FROM criteria_cards WHERE card_id = ? AND version = ?",
                              (cid, version)).fetchone()
    if row is None:
        raise StoreError(f"{cid} v{version} is not stored")
    return (CriteriaCard if row[0] == "criteria" else ContentCard).model_validate_json(row[1])


@dataclass
class Entry:
    card_id: str
    version: int
    kind: str
    status: str
    changed_rules: list[str]  # rules changed or gone since the card was stored

    @property
    def stale(self) -> bool:
        return bool(self.changed_rules)


def entries(conn: sqlite3.Connection) -> list[Entry]:
    conn = _conn(conn)
    out = []
    for cid, version, kind, status, hashes in conn.execute(
            "SELECT card_id, version, kind, status, rule_hashes FROM criteria_cards ORDER BY card_id, version"):
        changed = []
        for rid, h in json.loads(hashes).items():
            r = db.get_requirement(conn, rid)
            if r is None:
                changed.append(f"{rid} (gone)")
            elif rule_fingerprint(r) != h:
                changed.append(f"{rid} (changed)")
        out.append(Entry(cid, version, kind, status, changed))
    return out


def out_of_date(conn: sqlite3.Connection, card: Card) -> list[str]:
    """Rules this exact card version relies on that changed or disappeared since it was stored.
    A card that was never stored is checked against nothing and reported as such."""
    cid = card_id(card)
    row = _conn(conn).execute("SELECT content_hash FROM criteria_cards WHERE card_id = ? AND version = ?",
                              (cid, card.version)).fetchone()
    if row is None:
        raise StoreError(f"{cid} v{card.version} is not stored; store it first (python -m specpilot.card_store save)")
    if row[0] != card.content_hash():
        raise StoreError(f"{cid} v{card.version} differs from the stored version")
    return next(e.changed_rules for e in entries(conn) if (e.card_id, e.version) == (cid, card.version))


def latest_approved(conn: sqlite3.Connection) -> list[tuple[Card, list[str]]]:
    """Per card id, the newest approved version with a valid approval, and its out-of-date rules."""
    best: dict[str, Entry] = {}
    for e in entries(conn):
        if e.status == CardStatus.APPROVED.value:
            best[e.card_id] = e  # entries are ordered by version
    out = []
    for e in best.values():
        card = load(conn, e.card_id, e.version)
        if card.approval_valid:
            out.append((card, e.changed_rules))
    return out


def export(conn: sqlite3.Connection, out_dir: Path = EXPORT_DIR) -> tuple[list[Path], list[str]]:
    """Write the newest approved, up-to-date version of every card. Returns (files written, cards skipped)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for card, changed in latest_approved(conn):
        cid = card_id(card)
        if changed:
            skipped.append(f"{cid} v{card.version}: out of date ({', '.join(changed)})")
            continue
        path = out_dir / f"{cid}-v{card.version}.json"
        path.write_text(card.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written, skipped


def read_card_file(path: Path) -> Card:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    model = ContentCard if "rules" in data and "fields" in data and "criteria" not in data else CriteriaCard
    return model.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Store card versions in the team database and catch out-of-date cards.")
    p.add_argument("action", choices=["save", "list", "check", "export"])
    p.add_argument("card", nargs="?", help="card JSON file (for save)")
    p.add_argument("--db", required=True, help="the team database (specpilot.db)")
    p.add_argument("--out", default=str(EXPORT_DIR), help="folder for export")
    a = p.parse_args(argv)
    conn = db.connect(a.db)
    try:
        if a.action == "save":
            if not a.card:
                print("error: save needs a card file", file=sys.stderr)
                return 1
            card = read_card_file(Path(a.card))
            print(f"{card_id(card)} v{card.version} ({card.status.value}): {save(conn, card)}")
        elif a.action in ("list", "check"):
            rows = entries(conn)
            for e in rows:
                flag = f"   ⚑ OUT OF DATE: {', '.join(e.changed_rules)}" if e.stale else ""
                print(f"{e.card_id:<28} v{e.version:<3} {e.kind:<9} {e.status:<9}{flag}")
            stale = [e for e in rows if e.stale and e.status == CardStatus.APPROVED.value]
            if not rows:
                print("no cards stored")
            if a.action == "check":
                print(f"\n{len(stale)} approved card version(s) out of date" if stale else "\nall approved cards are up to date")
                return 1 if stale else 0
        else:
            written, skipped = export(conn, Path(a.out))
            for w in written:
                print(f"exported {w}")
            for s in skipped:
                print(f"⚑ not exported: {s}")
            if not written and not skipped:
                print("no approved cards to export")
    except (StoreError, OSError, ValidationError, json.JSONDecodeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
