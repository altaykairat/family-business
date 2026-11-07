"""SQLite projection of the event log: a star schema of facts and dimensions (decision 10, DW.1–DW.3), so any
relation is one GROUP BY. The database is a disposable cache (history/cache/cqa.sqlite, not in git): it is
rebuilt from the event log whenever the log changed, or built in memory for a past moment (time machine).

Dimensions: dim_conversation, dim_person, dim_checklist, dim_criterion, dim_run.
Facts:      fact_evaluation, fact_criterion, fact_appeal, fact_decision, fact_proposal, fact_task,
            fact_regression, feed (the history table).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from . import checklist as ck
from . import common, events, scoring, state
from . import conversation as cv
from .common import CQAError

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE dim_conversation (conversation_id TEXT PRIMARY KEY, dataset TEXT, channel TEXT, language TEXT,
  n_turns INT, duration_s INT, ending TEXT, operator_id TEXT);
CREATE TABLE dim_person (person_id TEXT PRIMARY KEY, name TEXT, role TEXT, team TEXT, demo INT, since TEXT);
CREATE TABLE dim_checklist (ref TEXT PRIMARY KEY, checklist_id TEXT, version INT, content_hash TEXT,
  created_at TEXT, author TEXT, reason TEXT, parent INT, status TEXT);
CREATE TABLE dim_criterion (rev_id TEXT PRIMARY KEY, criterion_id TEXT, rev INT, title TEXT, type TEXT);
CREATE TABLE dim_run (run_id TEXT PRIMARY KEY, dataset TEXT, checklist TEXT, variant TEXT, template TEXT,
  params_hash TEXT, created_at TEXT);
CREATE TABLE fact_evaluation (evaluation_id TEXT PRIMARY KEY, run_id TEXT, agent TEXT, conversation_id TEXT,
  checklist TEXT, variant TEXT, status TEXT, original_total REAL, final_total REAL, gross_original INT,
  gross_final INT, n_decisions INT, published INT, current INT, imported_at TEXT, calls INT, tokens_in INT,
  tokens_out INT);
CREATE TABLE fact_criterion (evaluation_id TEXT, conversation_id TEXT, run_id TEXT, agent TEXT, checklist TEXT,
  criterion_id TEXT, rev_id TEXT, original_answer TEXT, final_answer TEXT, original_points REAL,
  final_points REAL, weight REAL, effective_weight REAL, decided INT, changed INT, published INT, current INT);
CREATE TABLE fact_appeal (appeal_id TEXT PRIMARY KEY, evaluation_id TEXT, conversation_id TEXT, criterion_id TEXT,
  operator_id TEXT, filed_by TEXT, filed_at TEXT, status TEXT, assigned_to TEXT, decision_id TEXT, agent TEXT);
CREATE TABLE fact_decision (decision_id TEXT PRIMARY KEY, appeal_id TEXT, evaluation_id TEXT,
  conversation_id TEXT, criterion_id TEXT, criterion_rev TEXT, reviewer_id TEXT, operator_id TEXT, action TEXT,
  changed INT, model_answer TEXT, old_answer TEXT, new_answer TEXT, type TEXT, reason TEXT, decided_at TEXT,
  time_spent_s REAL, saw_counterargument INT, blind INT, revealed INT, overall_score REAL, total_delta REAL,
  precedent INT, agent TEXT, run_id TEXT, judged_on TEXT);
CREATE TABLE fact_proposal (proposal_id TEXT PRIMARY KEY, source TEXT, kind TEXT, criterion_id TEXT, title TEXT,
  status TEXT, created_by TEXT, created_at TEXT, resolved_by TEXT, resolved_at TEXT, resulting_version TEXT,
  edited INT);
CREATE TABLE fact_task (task_id TEXT PRIMARY KEY, kind TEXT, status TEXT, created_by TEXT, created_at TEXT,
  answered_at TEXT, provider TEXT);
CREATE TABLE fact_regression (regression_id TEXT PRIMARY KEY, ts TEXT, candidate TEXT, baseline TEXT,
  checklist_ref TEXT, passed INT, candidate_macro REAL, baseline_macro REAL, broken_precedents INT);
CREATE TABLE feed (seq INT, id TEXT, ts TEXT, type TEXT, actor TEXT, conversation_id TEXT, criterion_id TEXT,
  before TEXT, after TEXT, reason TEXT, links TEXT);
CREATE INDEX fc_crit ON fact_criterion (criterion_id);
CREATE INDEX fd_rev ON fact_decision (reviewer_id);
CREATE INDEX feed_conv ON feed (conversation_id);
"""


def _s(v):
    return None if v is None else (v if isinstance(v, (int, float)) and not isinstance(v, bool) else
                                   int(v) if isinstance(v, bool) else str(v))


def ending_of(conv: dict) -> str:
    patterns = common.setting("dimensions.ending_patterns", {})
    last = conv["transcript"][-1] if conv["transcript"] else None
    if not last:
        return "empty"
    if last["speaker"] != "system":
        return f"last:{last['speaker']}"
    for name, pats in patterns.items():
        if any(re.search(p, last["text"], re.I) for p in pats):
            return name
    return "system:other"


def materialize(st: state.State, conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    q = conn.execute
    for p in st.people.values():
        q("INSERT INTO dim_person VALUES (?,?,?,?,?,?)", (p["person_id"], p.get("name"), p.get("role"),
                                                         p.get("team"), int(bool(p.get("demo"))), p.get("since")))
    convs = {}
    for e in st.evaluations.values():
        convs.setdefault(e["conversation_id"], e.get("dataset") or "dev")
    for conv_id in set(st.operator_of) | set(st.suggested_operator):
        convs.setdefault(conv_id, "dev")
    for conv_id, dataset in convs.items():
        try:
            conv = cv.load(dataset, conv_id)
        except CQAError:
            continue
        tr = conv["transcript"]
        q("INSERT INTO dim_conversation VALUES (?,?,?,?,?,?,?,?)",
          (conv_id, dataset, conv["channel"], conv["language"], len(tr), tr[-1]["t"] if tr else 0,
           ending_of(conv), st.operator_of.get(conv_id)))
    refs = {e["checklist"] for e in st.evaluations.values()}
    for e in st.checklist_events:
        if e["type"] == "checklist_version_created":
            refs.add(e["checklist"])
    for cid in ck.ids():
        for v in ck.versions(cid):
            refs.add(ck.make_ref(cid, v))
    revs_done = set()
    for ref in sorted(refs):
        try:
            doc = st.doc(ref)
        except CQAError:
            continue
        m = doc["meta"]
        cid, ver = m.get("checklist_id"), m.get("version")
        q("INSERT OR REPLACE INTO dim_checklist VALUES (?,?,?,?,?,?,?,?,?)",
          (ck.make_ref(cid, ver), cid, ver, m["content_hash"], m.get("created_at"), m.get("author"), m.get("reason"),
           m.get("parent_version"), ck.status_of(cid, ver)))
        for crit_id, rev in ck.criterion_revs(doc).items():
            if rev in revs_done:
                continue
            revs_done.add(rev)
            c = ck.criterion(doc, crit_id)
            n = rev.split("@r")[-1]
            q("INSERT INTO dim_criterion VALUES (?,?,?,?,?)", (rev, crit_id, int(n) if n.isdigit() else None,
                                                              c.get("title"), c.get("type")))
    for r in st.runs.values():
        q("INSERT OR REPLACE INTO dim_run VALUES (?,?,?,?,?,?,?)", (r["run_id"], r.get("dataset"), r.get("checklist"),
                                                                   r.get("variant"), r.get("template"),
                                                                   r.get("params_hash"), r.get("ts")))
    for eid, e in st.evaluations.items():
        doc = st.doc(e["checklist"])
        fin = st.final(eid)
        decided = st.decisions_for(eid)
        usage = e.get("usage") or {}
        q("INSERT INTO fact_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (eid, e["run_id"], e["agent"], e["conversation_id"], e["checklist"], e.get("variant"), e.get("status"),
           fin["original"]["total"], fin["final"]["total"], int(fin["original"]["gross_error"]),
           int(fin["final"]["gross_error"]), len(decided), int(e["published"]), int(e["current"]), e["imported_at"],
           usage.get("calls"), usage.get("tokens_in"), usage.get("tokens_out")))
        revs = e.get("versions", {}).get("checklist", {}).get("criteria_revs") or ck.criterion_revs(doc)
        orig_rows = {r["criterion_id"]: r for r in scoring.score(doc, e["answers"])["rows"]}
        fin_rows = {r["criterion_id"]: r for r in fin["final"]["score"]["rows"]}
        for c in ck.active_criteria(doc):
            cid = c["id"]
            o, f = orig_rows[cid], fin_rows[cid]
            d = decided.get(cid)
            q("INSERT INTO fact_criterion VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (eid, e["conversation_id"], e["run_id"], e["agent"], e["checklist"], cid, revs.get(cid),
               _s(o["answer"]), _s(f["answer"]), o["points"], f["points"], c.get("weight"), f["effective_weight"],
               int(d is not None), int(bool(d and d["action"] == "change")), int(e["published"]), int(e["current"])))
    for a in st.appeals.values():
        ev = st.evaluations.get(a["evaluation_id"], {})
        q("INSERT INTO fact_appeal VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (a["appeal_id"], a["evaluation_id"], a["conversation_id"], a["criterion_id"], a.get("operator_id"),
           a.get("filed_by"), a.get("filed_at"), a["status"], a.get("assigned_to"), a.get("decision_id"),
           ev.get("agent")))
    for d in st.decisions.values():
        q("INSERT INTO fact_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (d["decision_id"], d.get("appeal_id"), d["evaluation_id"], d["conversation_id"], d["criterion_id"],
           d.get("criterion_rev"), d["reviewer_id"], d.get("operator_id") or st.operator_of.get(d["conversation_id"]),
           d["action"], int(d["action"] == "change"), _s(d.get("model_answer")), _s(d.get("old_answer")),
           _s(d.get("new_answer")), d.get("type"), d.get("reason"), d["decided_at"], d.get("time_spent_s"),
           int(bool(d.get("saw_counterargument"))), int(bool(d.get("blind"))), int(bool(d.get("revealed"))),
           d.get("overall_score"), d.get("total_delta"), int(d["decision_id"] in st.precedent), d.get("agent"),
           d.get("run_id"), d.get("judged_on")))
    for p in st.proposals.values():
        q("INSERT INTO fact_proposal VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (p["proposal_id"], p.get("source"), p.get("kind"), p.get("criterion_id"), p.get("title"), p["status"],
           p.get("created_by"), p.get("created_at"), p.get("resolved_by"), p.get("resolved_at"),
           p.get("resulting_version"), int(bool(p.get("edited")))))
    for t in st.tasks.values():
        q("INSERT INTO fact_task VALUES (?,?,?,?,?,?,?)", (t["task_id"], t["kind"], t["status"], t.get("created_by"),
                                                          t.get("created_at"), t.get("answered_at"), t.get("provider")))
    for r in st.regressions.values():
        q("INSERT INTO fact_regression VALUES (?,?,?,?,?,?,?,?,?)",
          (r["regression_id"], r["ts"], r.get("candidate"), r.get("baseline"), r.get("checklist_ref"),
           int(bool(r.get("passed"))), r.get("candidate_macro"), r.get("baseline_macro"),
           r.get("broken_precedents")))
    for e in st.events:
        row = state.feed_row(e)
        q("INSERT INTO feed VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (row["seq"], row["id"], row["ts"], row["type"], row["actor"], row["conversation_id"], row["criterion_id"],
           _s(row["before"]), _s(row["after"]), _s(row["reason"]), json.dumps(row["links"], ensure_ascii=False)))
    q("INSERT INTO meta VALUES ('fingerprint', ?)", (events.fingerprint(),))
    conn.commit()


def connect(until: Optional[str] = None) -> sqlite3.Connection:
    """A fresh projection: the cached file when the log has not changed, else rebuilt; in memory for `until`."""
    if until:
        conn = sqlite3.connect(":memory:")
        materialize(state.load(until), conn)
    else:
        path = common.CACHE / "cqa.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        fp = events.fingerprint()
        conn = None
        if path.exists():
            conn = sqlite3.connect(str(path))
            try:
                cur = conn.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
            except sqlite3.DatabaseError:
                cur = None
            if not cur or cur[0] != fp:
                conn.close()
                path.unlink()
                conn = None
        if conn is None:
            tmp = path.with_suffix(".tmp")
            if tmp.exists():
                tmp.unlink()
            c = sqlite3.connect(str(tmp))
            materialize(state.load(), c)
            c.close()
            tmp.replace(path)
            conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params: tuple = (), until: Optional[str] = None) -> list:
    """Read-only SQL over the projection (for analytics screens and ad-hoc questions)."""
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise CQAError("only SELECT queries are allowed on the projection")
    conn = connect(until)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
