"""SpecPilot UI: Overview, Specification (extraction agent), Testing results (testing agent),
Validation (vendor output), Human review (certificate decision) and Audits.

Run: python -m streamlit run app.py
"""
import csv
import hashlib
import io
import threading
from collections import Counter
from datetime import datetime

import streamlit as st

from specpilot import config, db
from specpilot.agents import extractor, tester
from specpilot.models import AuditEntry, CaseKind, Verdict
from specpilot.tools import pdf_export, pdf_ingest

VENDOR_DIR = config.ROOT / "output" / "vendor"
RESET = "reset the run"
DECISIONS = {"approve": "approved certificate", "rework": "requested rework", "reject": "rejected certificate"}

# Agent colours: one per agent, used on cards, steps and the sidebar
AGENTS = {
    "extractor": ("Extraction agent", "#2A78D6", "Reads the spec topics and writes rules with verified quotes"),
    "tester": ("Testing agent", "#7161E0", "Writes positive, negative and boundary test cases per topic"),
    "validator": ("Validation agent", "#D95926", "Compares the vendor output with the expected results"),
    "examiner": ("Human review", "#0B6E70", "Approves or rejects the certificate"),
}

st.set_page_config(page_title="SpecPilot", page_icon="🛫", layout="wide")
st.markdown(
    """<style>
    .sp-bar{background:linear-gradient(90deg,#1F3A5F 0%,#2A78D6 55%,#0B6E70 100%);color:#fff;padding:12px 18px;
            border-radius:12px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px}
    .sp-bar b{font-size:19px;font-weight:700}
    .sp-pill{background:rgba(255,255,255,.22);padding:3px 12px;border-radius:999px;font-size:12px}
    .sp-step{padding:7px 2px;border-radius:999px;font-size:12px;text-align:center;font-weight:600}
    .sp-ok{background:#E3F4E8;color:#14532D}.sp-off{background:#EEF0F3;color:#8A8F98}
    .sp-now{color:#fff;animation:sp-pulse 1.6s ease-in-out infinite}
    @keyframes sp-pulse{50%{opacity:.6}}
    .sp-card{border-radius:12px;padding:12px 14px;margin-bottom:10px;background:#fff;border:1px solid #E1E6EE;
             border-left:6px solid var(--c)}
    .sp-card h4{margin:0 0 2px;font-size:15px;color:var(--c)}
    .sp-card p{margin:2px 0;font-size:13px;color:#3D4C61}
    .sp-state{float:right;font-size:11px;font-weight:700;padding:2px 10px;border-radius:999px;color:#fff;background:var(--s)}
    .sp-run{border-color:var(--c);box-shadow:0 0 0 3px color-mix(in srgb,var(--c) 18%,transparent)}
    .sp-run .sp-state{animation:sp-pulse 1.2s ease-in-out infinite}
    .sp-mini{border-radius:8px;padding:6px 10px;margin:4px 0;font-size:12px;color:#fff;background:var(--c)}
    div[data-testid="stMetric"]{background:#fff;border:1px solid #E1E6EE;border-top:4px solid #2A78D6;border-radius:10px;padding:8px 12px}
    div[data-testid="stHorizontalBlock"] > div:nth-child(2) div[data-testid="stMetric"]{border-top-color:#7161E0}
    div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stMetric"]{border-top-color:#D95926}
    div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stMetric"]{border-top-color:#0B6E70}
    div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stMetric"]{border-top-color:#C04873}
    div[data-testid="stHorizontalBlock"] > div:nth-child(6) div[data-testid="stMetric"]{border-top-color:#A66A00}
    section[data-testid="stSidebar"]{background:linear-gradient(180deg,#1F3A5F 0%,#24476F 100%)}
    section[data-testid="stSidebar"] *{color:#fff}
    h3{color:#1F3A5F}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def jobs() -> dict[str, dict]:
    """Agent runs in background threads, shared by every browser tab: agent -> job."""
    return {}


def running(agent: str) -> bool:
    return jobs().get(agent, {}).get("state") == "running"


def start(agent: str, what: str, work) -> None:
    """Run work(conn, job) in a thread with its own connection (sqlite connections stay in their thread).

    work returns a one-line result; it may set job["step"] to say where it is.
    """
    job = {"what": what, "state": "running", "step": "starting", "started": datetime.now(), "result": "", "error": ""}
    jobs()[agent] = job

    def target():
        try:
            job["result"] = work(db.connect(), job)
            job["state"] = "done"
        except Exception as err:  # shown on the agent card
            job["state"], job["error"] = "error", str(err)
        job["ended"] = datetime.now()

    threading.Thread(target=target, daemon=True).start()


def validator_available() -> bool:
    try:
        from specpilot.agents import validator  # noqa: F401, PLC0415 - agent still being written
    except ImportError:
        return False
    return True


conn = db.connect()
documents = db.list_documents(conn)
rules = db.list_requirements(conn)
cases = db.list_topic_cases(conn)
audit = db.list_audit(conn)
section_titles = {(d["document"], s["number"]): s["title"] for d in documents for s in db.list_sections(conn, d["document"])}
titles = {num: t for (_, num), t in section_titles.items()}


def latest_run() -> str | None:
    row = conn.execute("SELECT run_id FROM findings ORDER BY id DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


run_id = latest_run()
findings = db.list_findings(conn, run_id) if run_id else []
# Uploads and decisions count from the last reset on; older ones stay in the audit log
since = next((i + 1 for i in range(len(audit) - 1, -1, -1) if audit[i].action == RESET), 0)
uploads = [e for e in audit[since:] if e.action == "uploaded vendor output"]
decisions = [e for e in audit[since:] if e.actor == "examiner" and e.action in DECISIONS.values()]


def latest_report():
    """The newest validation report PDF (output/validation/VAL-<time>/), or None."""
    from specpilot.agents import validator  # noqa: PLC0415

    found = sorted(validator.OUT_DIR.glob("VAL-*/validation_report.pdf"))
    return found[-1] if found else None


def label(number: str) -> str:
    return f"{number} {titles.get(number, '')}".strip()


def csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # utf-8-sig so Excel shows the German umlauts


def case_verdicts() -> dict[str, str]:
    """PASS / FAIL / MISSED per test case for the latest validation run. MISSED = no finding for the case."""
    by_case: dict[str, list] = {}
    for f in findings:
        by_case.setdefault(f.test_case_id, []).append(f)
    return {
        c.id: "MISSED" if c.id not in by_case else "FAIL" if any(f.verdict == Verdict.FAIL for f in by_case[c.id]) else "PASS"
        for c in cases
    }


STEP_AGENT = {"Rules extracted": "extractor", "Test cases": "tester", "Validation": "validator", "Human review": "examiner"}


def steps() -> list[tuple[str, str]]:
    done = [
        ("Specification", bool(documents)),
        ("Rules extracted", bool(rules)),
        ("Test cases", bool(cases)),
        ("Vendor output", bool(uploads)),
        ("Validation", bool(findings)),
        ("Human review", bool(decisions)),
    ]
    first_open = next((i for i, (_, ok) in enumerate(done) if not ok), len(done))
    return [(n, "now" if running(STEP_AGENT.get(n, "")) else "ok" if ok else "now" if i == first_open else "off")
            for i, (n, ok) in enumerate(done)]


def step_bar() -> None:
    for col, (name, state) in zip(st.columns(6), steps()):
        colour = AGENTS.get(STEP_AGENT.get(name, ""), ("", "#1F3A5F"))[1]
        css = {"ok": "sp-ok", "now": "sp-now", "off": "sp-off"}[state]
        style = f' style="background:{colour}"' if state == "now" else ""
        icon = "▶ " if running(STEP_AGENT.get(name, "")) else "✔ " if state == "ok" else ""
        col.markdown(f'<div class="sp-step {css}"{style}>{icon}{name}</div>', unsafe_allow_html=True)


def agent_state(agent: str) -> tuple[str, str, str]:
    """(state label, state colour, detail line) for an agent card."""
    job = jobs().get(agent)
    if job and job["state"] == "running":
        secs = int((datetime.now() - job["started"]).total_seconds())
        return "RUNNING", AGENTS[agent][1], f"{job['what']} · {job['step']} · {secs // 60}:{secs % 60:02d}"
    if job and job["state"] == "error":
        return "ERROR", "#B3261E", f"{job['what']}: {job['error']}"
    if job and job["state"] == "done":
        return "DONE", "#14804A", f"{job['what']}: {job['result']}"
    if agent == "validator" and not validator_available():
        return "NOT BUILT", "#8A8F98", "Coming next: the vendor file is saved until then"
    if agent == "examiner":
        if decisions:
            return "DONE", "#14804A", f"{decisions[-1].action} · {decisions[-1].at:%d.%m %H:%M}"
        return ("WAITING", "#A66A00", "Validation results are ready for your decision") if findings else ("IDLE", "#8A8F98", "Waits for validation results")
    last = next((e for e in reversed(audit[since:]) if e.actor == agent), None)
    return "IDLE", "#8A8F98", f"Last: {last.action} · {last.at:%d.%m %H:%M}" if last else "Not run yet"


def agent_panel(detailed: bool) -> None:
    """Agent status; refreshes itself every 2 s while an agent runs and reloads the page when one finishes."""
    active = [a for a in AGENTS if running(a)]
    seen = st.session_state.setdefault("seen_running", set())
    if seen - set(active):  # an agent finished since the last refresh: reload so the pages show its output
        st.session_state["seen_running"] = set(active)
        st.rerun(scope="app")
    st.session_state["seen_running"] = set(active)

    if not detailed:
        for agent, (name, colour, _) in AGENTS.items():
            state, scolour, line = agent_state(agent)
            if state in ("RUNNING", "ERROR", "WAITING"):
                st.markdown(f'<div class="sp-mini" style="--c:{scolour}">● {name}: {state.lower()}<br>{line}</div>', unsafe_allow_html=True)
        if not active:
            st.caption("No agent running")
        return

    cols = st.columns(2)
    for i, (agent, (name, colour, about)) in enumerate(AGENTS.items()):
        state, scolour, line = agent_state(agent)
        run = " sp-run" if state == "RUNNING" else ""
        cols[i % 2].markdown(
            f'<div class="sp-card{run}" style="--c:{colour};--s:{scolour}"><span class="sp-state">{state}</span>'
            f"<h4>{name}</h4><p>{about}</p><p><b>{line}</b></p></div>",
            unsafe_allow_html=True,
        )
    # A fragment rerun runs in another thread than the page that opened `conn`, and sqlite connections stay in their thread
    audit_now = db.list_audit(db.connect()) if active else []
    for agent in active:
        job = jobs()[agent]
        log_rows = [e for e in audit_now if e.actor == agent and e.at >= job["started"]]
        st.markdown(f"**Live log · {AGENTS[agent][0]}**")
        st.dataframe(
            [{"time": e.at.strftime("%H:%M:%S"), "step": e.action, "topic": e.subject_id, "model": e.model,
              "seconds": e.duration_s} for e in reversed(log_rows)] or [{"time": "", "step": "waiting for the first model answer ..."}],
            width="stretch", hide_index=True,
        )


def live(detailed: bool) -> None:
    every = "2s" if any(running(a) for a in AGENTS) else None
    st.fragment(run_every=every)(agent_panel)(detailed)


status = next((n for n, s in steps() if s == "now"), None)
status = f"Next: {status}" if status else decisions[-1].action.capitalize()
st.markdown(f'<div class="sp-bar"><b>🛫 SpecPilot</b><span>DTA EEL certification</span><span class="sp-pill">{status}</span></div>',
            unsafe_allow_html=True)

PAGES = ["Overview", "Agents", "Specification", "Testing results", "Validation", "Human review", "Audits"]
page = st.sidebar.radio("Pages", PAGES, label_visibility="collapsed")
st.sidebar.caption(f"{len(documents)} documents · {len({r.section for r in rules})} topics · {len(rules)} rules · {len(cases)} test cases")
with st.sidebar:
    st.markdown("**Agents**")
    live(detailed=False)


# --- pages ----------------------------------------------------------------------

def overview() -> None:
    st.subheader("Overview")
    step_bar()
    st.write("")

    verdicts = Counter(case_verdicts().values()) if findings else Counter()
    m = st.columns(6)
    m[0].metric("Documents", len(documents))
    m[1].metric("Topics extracted", len({r.section for r in rules}))
    m[2].metric("Rules (verified)", f"{len(rules)} ({sum(r.quote_verified for r in rules)})")
    m[3].metric("Test cases", len(cases))
    m[4].metric("Passed / failed", f"{verdicts['PASS']} / {verdicts['FAIL']}" if findings else "–")
    m[5].metric("Certificate", decisions[-1].action.split()[0] if decisions else "open")

    topics = tester.list_topics(conn)
    if topics:
        st.markdown("**Topics**")
        st.dataframe(
            [{"topic": t["number"], "title": t["title"], "rules": t["all_rules"], "testable": t["rules"],
              "flagged": sum(bool(r.flags) for r in rules if r.section == t["number"]), "test cases": t["cases"]}
             for t in topics],
            width="stretch", hide_index=True,
        )
    st.markdown("**Recent activity**")
    st.dataframe(
        [{"time": e.at.strftime("%d.%m %H:%M"), "actor": e.actor, "action": e.action, "subject": e.subject_id}
         for e in reversed(audit[-10:])],
        width="stretch", hide_index=True,
    )


def reset() -> None:
    c = db.connect()  # callbacks run before the script, in another thread than the page's connection
    db.reset_run(c)
    jobs().clear()
    for k in [k for k in st.session_state if k.startswith("topic|") or k == "tested_topics"]:
        del st.session_state[k]
    db.log(c, AuditEntry(actor="admin", action=RESET, detail="rules, test cases, expected results and findings deleted"))


def agents() -> None:
    head = st.columns([5, 1])
    head[0].subheader("Agents")
    busy = any(running(a) for a in AGENTS)
    with head[1].popover("⟲ Reset", width="stretch", disabled=busy, help="Wait for the running agent to finish" if busy else None):
        st.markdown("**Start fresh?**")
        st.caption("Deletes all extracted rules, test cases, validation results and the certificate decision. "
                   "The loaded spec PDFs and the audit log stay.")
        if st.button("Yes, reset", type="primary", on_click=reset, width="stretch"):
            st.rerun()
    step_bar()
    st.write("")
    live(detailed=True)


def specification() -> None:
    st.subheader("Specification")
    st.markdown("**Step 1 · Upload the spec PDFs**")
    files = st.file_uploader("Upload spec PDFs", type="pdf", accept_multiple_files=True, label_visibility="collapsed")
    load = st.button(f"▶ Start reading {len(files)} PDF(s)" if files else "▶ Start reading PDFs", type="primary",
                     disabled=not files, key="start_read")
    if not files:
        st.caption("Upload one or more PDFs, then click Start.")
    if files and load:
        config.SPEC_DIR.mkdir(parents=True, exist_ok=True)
        for f in files:
            path = config.SPEC_DIR / f.name
            path.write_bytes(f.getvalue())
            # the main spec keeps its fixed name, because rules cite it by that name
            name = config.SPEC_DOCUMENT if f.name == config.SPEC_PDF.name else path.stem
            with st.spinner(f"Reading {f.name} ..."):
                pages, sections = pdf_ingest.ingest(conn, path, name)
            db.log(conn, AuditEntry(actor="admin", action=f"loaded spec: {len(pages)} pages, {len(sections)} topics", subject_id=name))
        st.rerun()

    if not documents:
        st.info("Upload the Verfahrensbeschreibung (and any other spec PDFs) to start.")
        return
    st.divider()
    st.markdown("**Step 2 · Choose topics and start the extraction agent**")
    st.dataframe([{"document": d["document"], "pages": d["pages"], "topics": d["sections"]} for d in documents],
                 width="stretch", hide_index=True)

    names = [d["document"] for d in documents if d["sections"]]
    if not names:
        st.warning("No numbered topics found in the loaded PDFs.")
        return
    document = st.selectbox("Document", names, index=names.index(config.SPEC_DOCUMENT) if config.SPEC_DOCUMENT in names else 0)
    sections = db.list_sections(conn, document)
    extracted = Counter(r.section for r in rules if r.document == document)

    keys = {s["number"]: f"topic|{document}|{s['number']}" for s in sections}
    def tick(value: bool) -> None:
        for k in keys.values():
            st.session_state[k] = value

    chosen = [n for n, k in keys.items() if st.session_state.get(k)]
    head = st.columns([4, 1, 1])
    head[0].markdown(f"**Topics** · {len(chosen)} of {len(keys)} selected")
    head[1].button("☑ Select all", on_click=tick, args=(True,), width="stretch", disabled=len(chosen) == len(keys))
    head[2].button("☐ Clear", on_click=tick, args=(False,), width="stretch", disabled=not chosen)
    cols = st.columns(3)
    for i, s in enumerate(sections):
        k = keys[s["number"]]
        done = f" · ✔ {extracted[s['number']]} rules" if extracted[s["number"]] else ""
        cols[i % 3].checkbox(
            f"{s['number']} {s['title']}  (p.{s['first_page']}–{s['last_page']}){done}", key=k,
        )

    st.caption("The extraction agent reads each topic three times (1 page, 2 pages, whole topic), checks every quote "
               "against the PDF and flags contradictions. Re-running a topic replaces its rules and test cases.")
    if running("extractor"):
        st.info(f"Extraction agent is running: {jobs()['extractor']['what']}. Follow it on the Agents page.")
    elif st.button(f"▶ Start extraction ({len(chosen)} topic(s))", type="primary", disabled=not chosen, key="start_extract"):
        def work(c, job, topics=tuple(chosen)):
            job["step"] = "reading pages and asking Kolibri-1"
            found = extractor.run(c, topics, document)
            return f"{len(found)} rules, {sum(r.quote_verified for r in found)} quotes verified"

        st.session_state["tested_topics"] = chosen
        start("extractor", f"topics {', '.join(chosen)}", work)
        st.rerun()

    shown = [r for r in rules if r.document == document and (r.section in chosen or not chosen)]
    if not shown:
        return
    st.markdown("**Extracted rules**" + (" of the selected topics" if chosen else ""))
    table = st.dataframe(
        [{"rule": r.id, "page": r.page, "verified": "yes" if r.quote_verified else "no", "votes": r.votes,
          "kind": r.kind.value, "actor": r.actor.value, "flag": "contradiction" if r.flags else "", "statement": r.statement}
         for r in shown],
        width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
    )
    if table.selection.rows:
        r = shown[table.selection.rows[0]]
        with st.container(border=True):
            st.markdown(f"**{r.id} · page {r.page}** · {r.kind.value} · {r.actor.value} · {r.votes} vote(s)")
            st.markdown(f"> „{r.quote}“")
            st.markdown(r.statement)
            for fl in r.flags:
                st.warning(fl)


def testing_results() -> None:
    st.subheader("Testing results")
    ready = [t for t in tester.list_topics(conn) if t["rules"]]
    if not ready:
        st.info("No testable rules yet. Extract topics on the Specification page first.")
        return
    labels = {t["number"]: f"{t['number']} {t['title']} ({t['rules']} rules, {t['cases']} cases)" for t in ready}
    default = [n for n in st.session_state.get("tested_topics", []) if n in labels] or [t["number"] for t in ready if t["cases"]]
    selected = st.multiselect("Topics", list(labels), format_func=labels.get, default=default)
    n = st.number_input("Test cases per topic", min_value=1, max_value=30, value=tester.DEFAULT_CASES)
    if running("tester"):
        st.info(f"Testing agent is running: {jobs()['tester']['step']}. Follow it on the Agents page.")
    elif st.button("Generate test cases", type="primary", disabled=not selected):
        def work(c, job, topics=tuple(selected), per_topic=int(n)):
            made, problems = 0, []
            for i, section in enumerate(topics, start=1):
                job["step"] = f"topic {section} ({i}/{len(topics)})"
                try:
                    res = tester.generate(c, section, per_topic)
                    made += len(res.cases)
                    if res.uncovered:
                        problems.append(f"{section}: no case for {', '.join(res.uncovered)}")
                except Exception as err:  # one failing topic must not stop the others
                    problems.append(f"{section}: {err}")
            return f"{made} test cases" + (f" · {'; '.join(problems)}" if problems else "")

        start("tester", f"{len(selected)} topic(s)", work)
        st.rerun()

    chosen = [c for c in cases if c.section in selected]
    if not chosen:
        st.caption("No test cases for the selected topics yet.")
        return

    kinds = Counter(c.kind.value for c in chosen)
    topic_rules = {r.id for s in selected for r in tester.topic_rules(conn, s)}
    covered = {i for c in chosen for i in c.requirement_ids} & topic_rules
    m = st.columns(5)
    m[0].metric("Test cases", len(chosen))
    m[1].metric("Positive", kinds["positive"])
    m[2].metric("Negative", kinds["negative"])
    m[3].metric("Boundary", kinds["boundary"])
    m[4].metric("Rules covered", f"{len(covered)} / {len(topic_rules)}")
    if topic_rules - covered:
        st.warning(f"No test case for {', '.join(sorted(topic_rules - covered))}")

    vendor, expected = tester.export_rows(conn, selected)
    b = st.columns(2)
    b[0].download_button("⬇ Test cases (PDF, for the vendor)", pdf_export.test_cases_pdf(vendor), "test_cases.pdf",
                         "application/pdf", width="stretch")
    b[1].download_button("⬇ Expected output (PDF)", pdf_export.expected_results_pdf(expected), "expected_results.pdf",
                         "application/pdf", width="stretch")

    kind = st.segmented_control("Kind", ["all"] + [k.value for k in CaseKind], default="all")
    st.dataframe(
        [{"case": c.id, "topic": c.section, "kind": c.kind.value, "rules": ", ".join(c.requirement_ids), "title": c.title,
          "given": c.given, "steps": "\n".join(c.steps), "expected": c.expected}
         for c in chosen if kind in (None, "all", c.kind.value)],
        width="stretch", hide_index=True,
    )


def validation() -> None:
    st.subheader("Validation")
    st.markdown("**Step 1 · Upload the vendor's actual output**")
    file = st.file_uploader("Vendor output (PDF)", type=["pdf"], label_visibility="collapsed")
    data = file.getvalue() if file is not None else b""
    if file is not None:
        sha = hashlib.sha256(data).hexdigest()
        records = []
        try:
            from specpilot.agents import validator  # noqa: PLC0415

            tmp = VENDOR_DIR / f".check-{sha[:12]}.pdf"
            VENDOR_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            records = list(validator.read_vendor_pdf(tmp).outputs)
            tmp.unlink()
        except Exception as err:
            st.error(str(err))
        c = st.columns(3)
        c[0].metric("Size", f"{len(data):,} bytes")
        c[1].metric("Test case ids found", len(records))
        c[2].metric("sha256", f"{sha[:6]}…{sha[-4:]}")
    elif uploads:
        st.caption(f"Last file: {uploads[-1].subject_id} · {uploads[-1].at:%d.%m %H:%M}")

    st.markdown("**Step 2 · Start the validation agent**")
    if running("validator"):
        st.info("Validation agent is running. Follow it on the Agents page.")
    else:
        clicked = st.button("▶ Start validation", type="primary", disabled=not data or not cases, key="start_validate")
        if not cases:
            st.caption("Generate test cases first: the output is compared with their expected results.")
        elif not data:
            st.caption("Upload the vendor output, then click Start.")
        if clicked:
            VENDOR_DIR.mkdir(parents=True, exist_ok=True)
            path = VENDOR_DIR / file.name
            path.write_bytes(data)
            db.log(conn, AuditEntry(actor="admin", action="uploaded vendor output", subject_id=file.name,
                                    detail=f"sha256 {sha}, {len(records)} records"))
            if not validator_available():
                st.warning("File saved. The validation agent is not connected yet, so no results were produced.")
            else:
                def work(c, job):
                    from specpilot.agents import validator  # noqa: PLC0415
                    job["step"] = "comparing with the expected results"
                    v = validator.validate(c, path)
                    return f"{v.verdict}: {v.count('PASS')} passed, {v.count('FAIL')} failed, {v.count('MISSING') + v.count('UNCLEAR')} missing"

                start("validator", file.name, work)
                st.rerun()

    st.divider()
    report = latest_report()
    if report:
        st.download_button("⬇ Validation report (PDF)", report.read_bytes(), report.name, "application/pdf",
                           type="primary", key="dl_validation_report")
        st.caption(f"Run {report.parent.name}: failed cases with the spec text behind them, missing information and a summary.")
    if not findings:
        st.info("No validation results yet.")
        return
    verdicts = case_verdicts()
    count = Counter(verdicts.values())
    m = st.columns(4)
    m[0].metric("Test cases", len(verdicts))
    m[1].metric("✅ Passed", count["PASS"])
    m[2].metric("❌ Failed", count["FAIL"])
    m[3].metric("⚪ Missed", count["MISSED"])
    show = st.segmented_control("Show", ["all", "PASS", "FAIL", "MISSED"], default="all")
    by_case = {c.id: c for c in cases}
    st.dataframe(
        [{"case": cid, "topic": by_case[cid].section, "title": by_case[cid].title, "verdict": v}
         for cid, v in verdicts.items() if show in (None, "all", v)],
        width="stretch", hide_index=True,
    )
    failed = [f for f in findings if f.verdict == Verdict.FAIL]
    if failed:
        st.markdown("**Why cases failed**")
        st.dataframe(
            [{"case": f.test_case_id, "rule": f.requirement_id or "", "layer": f.layer.value, "field": f.field or "",
              "expected": f.expected or "", "actual": f.actual or "", "message": f.message,
              "cause": f.cause + (" (unconfirmed)" if f.cause and not f.cause_confirmed else "")} for f in failed],
            width="stretch", hide_index=True,
        )


def human_review() -> None:
    st.subheader("Human review")
    verdicts = case_verdicts() if findings else {}
    count = Counter(verdicts.values())
    with st.container(border=True):
        st.markdown("**Validation agent result**")
        if findings:
            failed_rules = sorted({f.requirement_id for f in findings if f.verdict == Verdict.FAIL and f.requirement_id})
            st.markdown(
                f"- Run `{run_id}` on {uploads[-1].subject_id if uploads else 'vendor output'}\n"
                f"- {count['PASS']} passed · {count['FAIL']} failed · {count['MISSED']} missed of {len(verdicts)} cases\n"
                + (f"- Failed rules: {', '.join(failed_rules)}" if failed_rules else "- No failed rules")
            )
        else:
            st.markdown("No validation results yet. Validate the vendor output first.")

    reason = st.text_area("Reason for your decision (required)")
    b = st.columns(3)
    pressed = None
    if b[0].button("✅ Approve certificate", type="primary", disabled=not findings or not reason.strip(), width="stretch"):
        pressed = "approve"
    if b[1].button("↺ Request rework", disabled=not findings or not reason.strip(), width="stretch"):
        pressed = "rework"
    if b[2].button("❌ Reject", disabled=not findings or not reason.strip(), width="stretch"):
        pressed = "reject"
    if pressed:
        db.log(conn, AuditEntry(actor="examiner", action=DECISIONS[pressed], subject_id=run_id or "",
                                detail=f"{count['PASS']} pass / {count['FAIL']} fail / {count['MISSED']} missed. {reason.strip()}"))
        st.rerun()
    st.caption("Every decision is written to the audit log with its reason.")

    if decisions:
        st.markdown("**Decisions**")
        st.dataframe(
            [{"time": e.at.strftime("%d.%m.%Y %H:%M"), "decision": e.action, "run": e.subject_id, "reason": e.detail}
             for e in reversed(decisions)],
            width="stretch", hide_index=True,
        )


def audits() -> None:
    st.subheader("Audits")
    f = st.columns(3)
    actor = f[0].selectbox("Actor", ["all"] + sorted({e.actor for e in audit}))
    subject = f[1].selectbox("Subject", ["all"] + sorted({e.subject_id for e in audit if e.subject_id}))
    text = f[2].text_input("Action contains")
    shown = [e for e in reversed(audit)
             if (actor == "all" or e.actor == actor) and (subject == "all" or e.subject_id == subject)
             and text.lower() in e.action.lower()]
    rows = [{"time": e.at.strftime("%d.%m.%Y %H:%M:%S"), "actor": e.actor, "action": e.action, "subject": e.subject_id,
             "model": e.model, "cache": ("hit" if e.cache_hit else "miss") if e.model else "",
             "seconds": e.duration_s, "detail": e.detail} for e in shown]
    st.dataframe(rows, width="stretch", hide_index=True)
    if rows:
        st.download_button("Export CSV", csv_bytes(rows, list(rows[0])), f"audit_{datetime.now():%Y%m%d}.csv", "text/csv")


{
    "Overview": overview, "Agents": agents, "Specification": specification, "Testing results": testing_results,
    "Validation": validation, "Human review": human_review, "Audits": audits,
}[page]()
