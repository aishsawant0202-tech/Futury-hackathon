"""Dashboard server: serves the page and the state, takes uploads, refuses what it must."""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from specpilot import db
from specpilot.agents import admin
from specpilot.gui import server as gui
from scripts.make_verification_scenarios import text_pdf
from tests.test_rule_picker import GOLD_RULES, NOISE


@pytest.fixture
def site(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(admin, "run_pipeline", lambda conn, pdf, **kw: started.append(pdf))
    conn = db.connect(tmp_path / "team.db")
    for r in GOLD_RULES + NOISE:
        db.save_requirement(conn, r)
    conn.close()
    srv = gui.Server(0, tmp_path / "team.db", out_dir=tmp_path / "out", upload_dir=tmp_path / "uploads",
                     vendor_dir=tmp_path / "vendor")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", srv, started
    srv.shutdown()
    srv.server_close()


def call(url, data: bytes | None = None, method: str | None = None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method)) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_page_and_state(site):
    url, _, _ = site
    status, body = call(url + "/")
    assert status == 200 and b"SpecPilot" in body and b"Missing information" in body
    s = json.loads(call(url + "/api/state")[1])
    assert s["running"] is False and s["run"] is None and s["card"] is None and s["submissions"] == []
    assert len(s["rules"]) == len(GOLD_RULES + NOISE)
    a = s["accounting"]
    assert a["total"] == 13 and a["about"] + a["other_messages"] == 13 and len(a["picked"]) + a["excluded"] == a["about"]
    assert s["totals"]["error_rate"] is None


def test_state_of_an_empty_database(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    s = gui.state(conn)
    assert s["rules"] == [] and s["accounting"] is None and s["totals"]["submissions"] == 0


def test_spec_upload_starts_a_run(site):
    url, srv, started = site
    status, body = call(url + "/api/spec?name=spec.pdf", b"%PDF-1.4 test", "POST")
    assert status == 202 and json.loads(body) == {"started": "spec.pdf"}
    srv.worker._thread.join(5)
    assert started == [srv.upload_dir / "spec.pdf"] and (srv.upload_dir / "spec.pdf").read_bytes() == b"%PDF-1.4 test"


def test_only_one_run_at_a_time(site, monkeypatch):
    url, srv, _ = site
    monkeypatch.setattr(gui.Worker, "running", property(lambda self: True))
    assert call(url + "/api/spec?name=spec.pdf", b"%PDF-1.4", "POST")[0] == 409


@pytest.mark.parametrize("name, body", [("notes.txt", b"%PDF-1.4"), ("../evil.pdf", b"x"), ("report.pdf", b"not a pdf"),
                                        (".hidden.pdf", b"%PDF-1.4"), ("", b"%PDF")])
def test_bad_uploads_are_refused(site, name, body):
    url, srv, _ = site
    assert call(f"{url}/api/vendor?name={quote(name)}", body, "POST")[0] == 400
    assert not srv.vendor_dir.exists() or not any(srv.vendor_dir.iterdir())


def test_vendor_upload_is_verified_and_stored(site):
    url, srv, _ = site
    pdf = text_pdf(["Vendor: ACME", "Dummy file: REQ-41-X-v1-seed1", "Case: DUMMY-0001 | Meldung: ja"], srv.out_dir / "r.pdf")
    status, body = call(url + "/api/vendor?name=acme.pdf", pdf.read_bytes(), "POST")
    assert status == 200
    s = json.loads(call(url + "/api/state")[1])
    [sub] = s["submissions"]
    assert sub["id"] == json.loads(body)["submission"] and sub["vendor"] == "ACME"
    assert sub["verdict"] == "MISSING INFORMATION" and sub["has_missing_information"]  # no answers for that dummy file
    assert sub["flags"] and all(f["kind"] == "missing" for f in sub["flags"])


def test_downloads_only_from_the_output_folder(site, tmp_path):
    url, srv, _ = site
    inside = srv.out_dir / "dummies" / "x.stripped.csv"
    inside.parent.mkdir(parents=True)
    inside.write_text("neutral_id\nDUMMY-0001\n", encoding="utf-8")
    status, body = call(f"{url}/download?file={quote(str(inside))}")
    assert status == 200 and body.startswith(b"neutral_id")
    for outside in (tmp_path / "team.db", srv.out_dir / ".." / "team.db", Path("specpilot.db").resolve()):
        assert call(f"{url}/download?file={quote(str(outside))}")[0] == 403


def test_a_name_with_folders_cannot_leave_the_vendor_folder(site, tmp_path):
    url, srv, _ = site
    pdf = text_pdf(["Vendor: ACME"], tmp_path / "src.pdf").read_bytes()
    assert call(f"{url}/api/vendor?name={quote('../../escaped.pdf')}", pdf, "POST")[0] == 200
    assert (srv.vendor_dir / "escaped.pdf").exists() and not (tmp_path / "escaped.pdf").exists()
