"""SpecPilot dashboard: a local web page over the pipeline. Python standard library only.

Run: python -m specpilot.gui.server [--port 8765] [--db specpilot.db]      then open http://127.0.0.1:8765

    GET  /                    the page (index.html next to this file)
    GET  /api/state           everything the page shows, as JSON: run, rules, card, submissions, checks, audit
    GET  /download?file=...   a file the pipeline wrote (only inside output/)
    POST /api/spec?name=...   body = a spec PDF: saved to data/specs/uploads/, starts a full run in the background
    POST /api/vendor?name=... body = a vendor report PDF: saved to data/vendor/, verified and stored at once
    POST /api/demo            writes the three invented SCENARIO vendor reports and verifies them

The page only shows and uploads; every decision is made by the administrative agent and the checks.
Only one run at a time. The server listens on 127.0.0.1, so it is reachable from this computer only.
"""
import argparse
import json
import logging
import sqlite3
import threading
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from specpilot import card_store, config, db, submissions
from specpilot.agents import admin, verification
from specpilot.tools import rule_picker

log = logging.getLogger("specpilot.gui")

PAGE = Path(__file__).with_name("index.html")
UPLOAD_DIR = config.SPEC_DIR / "uploads"
MAX_UPLOAD = 200 * 1024 * 1024
AUDIT_ROWS = 200


# --- what the page shows ----------------------------------------------------------------------------------------------

def _accounting(conn: sqlite3.Connection, card_id: str) -> dict | None:
    """116 extracted -> other messages / about the message -> picked / left out, with the reasons counted."""
    try:
        p = rule_picker.pick(conn, card_id)
    except rule_picker.PickError:
        return None
    reasons = Counter(reason for e in p.excluded for reason in e.reasons)
    return {"card_id": card_id, "reason_code": p.spec.reason_code,
            "total": len(p.picked) + len(p.excluded) + p.other_messages, "other_messages": p.other_messages,
            "about": len(p.picked) + len(p.excluded), "picked": p.rule_ids, "excluded": len(p.excluded),
            "reasons": reasons.most_common()}


def _card(conn: sqlite3.Connection) -> dict | None:
    """The newest stored criteria card, with its conditions and whether its rules changed since."""
    entries = [e for e in card_store.entries(conn) if e.kind == "criteria"]
    if not entries:
        return None
    e = max(entries, key=lambda x: (x.card_id == "REQ-41-PRECONDITION", x.version))
    card = card_store.load(conn, e.card_id, e.version)
    return {"id": card.id, "version": card.version, "status": card.status.value, "reviewer": card.reviewer,
            "reviewed_at": card.reviewed_at.isoformat(timespec="minutes") if card.reviewed_at else "",
            "logic": card.logic, "changed_rules": e.changed_rules,
            "criteria": [{"id": c.id, "text": c.describe(), "rules": c.source_rule_ids, "quote": c.quote,
                          "readings": len(c.alternatives) + 1} for c in card.criteria],
            "unused": [{"rule_id": u.rule_id, "reason": u.reason} for u in card.unused_rules],
            "missing_facts": [m.needed for m in card.missing_facts]}


def state(conn: sqlite3.Connection, running: bool = False) -> dict:
    run = admin.latest_run(conn)
    rules = db.list_requirements(conn)
    subs = submissions.list_all(conn)
    for s in subs:
        s["flags"] = submissions.flags(conn, s["id"])
    audit = conn.execute("SELECT at, actor, action, subject_id, model, cache_hit, duration_s FROM audit "
                         "ORDER BY id DESC LIMIT ?", (AUDIT_ROWS,)).fetchall()
    return {
        "running": running,
        "run": run,
        "spec": {"document": config.SPEC_DOCUMENT,
                 "pages": conn.execute("SELECT COUNT(*) FROM spec_pages").fetchone()[0],
                 "sections": conn.execute("SELECT COUNT(*) FROM spec_sections").fetchone()[0]},
        "rules": [{"id": r.id, "section": r.section, "page": r.page, "verified": r.quote_verified, "votes": r.votes,
                   "actor": r.actor.value, "kind": r.kind.value, "codes": r.reason_codes, "statement": r.statement,
                   "quote": r.quote, "flags": r.flags} for r in rules],
        "accounting": _accounting(conn, "REQ-41-PRECONDITION") if rules else None,
        "card": _card(conn),
        "submissions": subs,
        "totals": submissions.totals(conn),
        "audit": [dict(a) for a in audit],
    }


# --- the background run -----------------------------------------------------------------------------------------------

class Worker:
    """At most one pipeline run at a time, in its own thread with its own database connection."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, pdf: Path) -> bool:
        with self._lock:
            if self.running:
                return False
            self._thread = threading.Thread(target=self._run, args=(pdf,), daemon=True)
            self._thread.start()
            return True

    def _run(self, pdf: Path) -> None:
        conn = db.connect(self.db_path)
        try:
            admin.run_pipeline(conn, pdf)
        finally:
            conn.close()


# --- HTTP ------------------------------------------------------------------------------------------------------------

def _safe_name(raw: str) -> str | None:
    """Only a plain file name ending in .pdf: no folders, nothing hidden."""
    name = Path(raw or "").name
    return name if name.lower().endswith(".pdf") and not name.startswith(".") else None


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, fmt, *args):  # quiet: one line per request is noise in the terminal
        log.debug(fmt, *args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.server.db_path)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            conn = self._conn()
            try:
                self._json(state(conn, self.server.worker.running))
            finally:
                conn.close()
        elif url.path == "/download":
            self._download(parse_qs(url.query).get("file", [""])[0])
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _download(self, file: str) -> None:
        path = Path(file).resolve()
        if not path.is_relative_to(self.server.out_dir.resolve()) or not path.is_file():
            self._json({"error": "only files the pipeline wrote can be downloaded"}, HTTPStatus.FORBIDDEN)
            return
        kind = "text/csv; charset=utf-8" if path.suffix == ".csv" else "application/octet-stream"
        self._send(HTTPStatus.OK, path.read_bytes(), kind, {"Content-Disposition": f'attachment; filename="{path.name}"'})

    def _body_pdf(self) -> tuple[str, bytes] | None:
        name = _safe_name(parse_qs(urlparse(self.path).query).get("name", [""])[0])
        length = int(self.headers.get("Content-Length") or 0)
        if not name or not 0 < length <= MAX_UPLOAD:
            self._json({"error": "send one PDF file (name ending in .pdf, at most 200 MB)"}, HTTPStatus.BAD_REQUEST)
            return None
        body = self.rfile.read(length)
        if not body.startswith(b"%PDF"):
            self._json({"error": f"{name} is not a PDF"}, HTTPStatus.BAD_REQUEST)
            return None
        return name, body

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/demo":
            self._demo()
            return
        if route not in ("/api/spec", "/api/vendor"):
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        got = self._body_pdf()
        if got is None:
            return
        name, body = got
        if route == "/api/spec":
            if self.server.worker.running:
                self._json({"error": "a run is already in progress"}, HTTPStatus.CONFLICT)
                return
            path = self.server.upload_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            self.server.worker.start(path)
            self._json({"started": name}, HTTPStatus.ACCEPTED)
        else:
            path = self.server.vendor_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            conn = self._conn()
            try:
                ids = admin.verify_reports(conn, [path])
            finally:
                conn.close()
            self._json({"submission": ids[0]})

    def _demo(self) -> None:
        from scripts.make_verification_scenarios import make  # demo only: invented data, clearly labelled SCENARIO
        made = make(self.server.out_dir / "verification_scenarios", self.server.vendor_dir / "scenarios")
        conn = self._conn()
        try:
            ids = admin.verify_reports(conn, [made["certified"], made["rejected"], made["missing_information"]],
                                       expected_dir=made["expected"], dummy_stem="REQ-41-PRECONDITION-v1-seed4711")
        finally:
            conn.close()
        self._json({"submissions": ids})


class Server(ThreadingHTTPServer):
    def __init__(self, port: int, db_path: Path, out_dir: Path = admin.OUT_DIR,
                 upload_dir: Path = UPLOAD_DIR, vendor_dir: Path = verification.VENDOR_DIR):
        super().__init__(("127.0.0.1", port), Handler)
        self.db_path, self.out_dir, self.upload_dir, self.vendor_dir = Path(db_path), out_dir, upload_dir, vendor_dir
        self.worker = Worker(self.db_path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="SpecPilot dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=str(config.DB_PATH))
    a = p.parse_args(argv)
    conn = db.connect(a.db)
    admin.finish_interrupted(conn)  # a run cut off when the server last stopped
    conn.close()
    server = Server(a.port, Path(a.db))
    print(f"SpecPilot dashboard: http://127.0.0.1:{server.server_address[1]}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
