"""Current state = replay of the event log (decision 8). Nothing here is stored separately: final scores,
precedents, appeal statuses, active versions — all are computed from events, so the history is the only truth.

`load(until=None)` replays everything (or everything up to a moment — the "time machine", VC.9).
The result is cached in-process by the log fingerprint.
"""

from __future__ import annotations

from typing import Optional

from . import checklist as ck
from . import events, scoring
from .common import CQAError

ROLES = ("operator", "reviewer", "admin", "supervisor", "pipeline")
PREFIX_ROLES = (("op-", "operator"), ("r-", "reviewer"), ("a-", "admin"), ("s-", "supervisor"))
PIPELINE_ACTORS = ("system", "orchestrator", "pipeline")
APPEAL_TYPES = {
    "A": "Ошибка оценки",
    "B": "Пробел в правилах",
    "C": "Несоразмерный вес",
    "D": "Апелляция необоснованна",
}


class State:
    def __init__(self):
        self.people: dict = {}
        self.operator_of: dict = {}          # conversation_id → operator_id (confirmed)
        self.suggested_operator: dict = {}   # conversation_id → {operator_id, name}
        self.runs: dict = {}
        self.evaluations: dict = {}
        self.latest_by_key: dict = {}        # (run, agent, conv) → evaluation_id
        self.published: dict = {}            # conversation_id → evaluation_id
        self.appeals: dict = {}
        self.decisions: dict = {}
        self.precedent: dict = {}            # decision_id → {by, ts, note} when active
        self.reviewer_settings: dict = {}
        self.reveals: list = []
        self.tasks: dict = {}
        self.proposals: dict = {}
        self.regressions: dict = {}
        self.metric_reports: dict = {}
        self.gt_versions: list = []
        self.digests: list = []
        self.checklist_events: list = []
        self.config_versions: list = []
        self.datasets: list = []
        self.events: list = []
        self._docs: dict = {}

    # --- helpers -----------------------------------------------------------------
    def doc(self, ref: str) -> dict:
        if ref not in self._docs:
            self._docs[ref] = ck.load(ref.replace("@v", "@"))
        return self._docs[ref]

    def role_of(self, actor: Optional[str]) -> str:
        if not actor:
            return "unknown"
        if actor in self.people:
            return self.people[actor]["role"]
        if actor in PIPELINE_ACTORS or actor.startswith(("llm:", "agent:")):
            return "pipeline"
        for prefix, role in PREFIX_ROLES:
            if actor.startswith(prefix):
                return role
        return "unknown"

    def evaluation(self, evaluation_id: str) -> dict:
        ev = self.evaluations.get(evaluation_id)
        if ev is None:
            raise CQAError(f"evaluation {evaluation_id} not found", "not_found")
        return ev

    def appeal(self, appeal_id: str) -> dict:
        ap = self.appeals.get(appeal_id)
        if ap is None:
            raise CQAError(f"appeal {appeal_id} not found", "not_found")
        return ap

    def decisions_for(self, evaluation_id: str) -> dict:
        """criterion_id → latest decision on this evaluation."""
        out = {}
        for d in self.decisions.values():
            if d["evaluation_id"] == evaluation_id:
                out[d["criterion_id"]] = d  # dict keeps insertion (= time) order
        return out

    def final(self, evaluation_id: str) -> dict:
        """Original model evaluation + reviewer decisions on top → recomputed with the same scoring (H.3)."""
        ev = self.evaluation(evaluation_id)
        doc = self.doc(ev["checklist"])
        decided = self.decisions_for(evaluation_id)
        answers = dict(ev["answers"])
        for cid, d in decided.items():
            answers[cid] = d["new_answer"]
        original = ev["score"] if ev.get("score") else scoring.score(doc, ev["answers"])
        final_score = scoring.score(doc, answers)
        return {
            "evaluation_id": evaluation_id,
            "conversation_id": ev["conversation_id"],
            "checklist": ev["checklist"],
            "original": {"answers": ev["answers"], "total": original["total"],
                         "gross_error": original["gross_error"]},
            "final": {"answers": answers, "total": final_score["total"], "gross_error": final_score["gross_error"],
                      "score": final_score},
            "decisions": {cid: d["decision_id"] for cid, d in decided.items()},
            "changed_criteria": sorted(cid for cid, d in decided.items() if d["action"] == "change"),
        }

    def human_truth(self) -> dict:
        """(conversation_id, criterion_id) → latest reviewer decision (any evaluation of the conversation)."""
        out = {}
        for d in self.decisions.values():
            out[(d["conversation_id"], d["criterion_id"])] = d
        return out

    def active_precedents(self) -> list:
        return [self.decisions[did] for did in self.precedent if did in self.decisions]


# --- replay ---------------------------------------------------------------------------

def _apply(s: State, e: dict) -> None:
    t = e["type"]
    if t == "person_registered":
        s.people[e["person_id"]] = {k: e.get(k) for k in ("person_id", "name", "role", "team", "languages",
                                                            "status", "demo")} | {"since": e["ts"]}
    elif t == "person_updated":
        if e["person_id"] in s.people:
            s.people[e["person_id"]].update(e.get("fields") or {})
    elif t == "operator_suggested":
        s.suggested_operator[e["conversation_id"]] = {"operator_id": e["operator_id"], "name": e.get("name"),
                                                      "method": e.get("method")}
    elif t == "operator_assigned":
        s.operator_of[e["conversation_id"]] = e["operator_id"]
    elif t == "run_prepared":
        s.runs[e["run_id"]] = {"run_id": e["run_id"], "dataset": e.get("dataset"), "checklist": e.get("checklist"),
                               "variant": e.get("variant"), "template": e.get("template"),
                               "params_hash": e.get("params_hash"), "agents": e.get("agents"),
                               "conversations": e.get("conversations"), "versions": e.get("versions"), "ts": e["ts"]}
    elif t == "evaluation_imported":
        key = (e["run_id"], e["agent"], e["conversation_id"])
        prev = s.latest_by_key.get(key)
        if prev:
            s.evaluations[prev]["current"] = False
        s.evaluations[e["evaluation_id"]] = {**{k: v for k, v in e.items() if k not in ("prev", "hash")},
                                             "current": True, "published": False, "imported_at": e["ts"]}
        s.latest_by_key[key] = e["evaluation_id"]
    elif t == "evaluation_published":
        old = s.published.get(e["conversation_id"])
        if old and old in s.evaluations:
            s.evaluations[old]["published"] = False
        s.published[e["conversation_id"]] = e["evaluation_id"]
        if e["evaluation_id"] in s.evaluations:
            s.evaluations[e["evaluation_id"]]["published"] = True
    elif t == "appeal_filed":
        s.appeals[e["appeal_id"]] = {**{k: e.get(k) for k in ("appeal_id", "evaluation_id", "conversation_id",
                                                              "criterion_id", "operator_id", "comment", "turn_idx",
                                                              "attachments")},
                                     "filed_by": e["actor"], "filed_at": e["ts"], "status": "open",
                                     "assigned_to": None, "assigned_at": None, "opened_at": None,
                                     "decision_id": None}
    elif t == "appeal_assigned":
        ap = s.appeals.get(e["appeal_id"])
        if ap:
            ap.update(assigned_to=e["reviewer_id"], assigned_at=e["ts"], status="assigned")
    elif t == "appeal_opened":
        ap = s.appeals.get(e["appeal_id"])
        if ap and not ap["opened_at"]:
            ap["opened_at"] = e["ts"]
    elif t == "appeal_closed":
        ap = s.appeals.get(e["appeal_id"])
        if ap:
            ap.update(status="closed", closed_reason=e.get("reason"))
    elif t == "model_answer_revealed":
        s.reveals.append({k: e.get(k) for k in ("appeal_id", "evaluation_id", "criterion_id")} |
                         {"reviewer_id": e["actor"], "ts": e["ts"]})
    elif t == "reviewer_settings_changed":
        s.reviewer_settings.setdefault(e["reviewer_id"], {}).update(e.get("settings") or {})
    elif t == "decision_made":
        d = {k: v for k, v in e.items() if k not in ("prev", "hash", "seq", "id")}
        d["decided_at"] = e["ts"]
        d["type"] = e.get("appeal_type")
        s.decisions[e["decision_id"]] = d
        if e.get("appeal_id") and e["appeal_id"] in s.appeals:
            s.appeals[e["appeal_id"]].update(status="decided", decision_id=e["decision_id"])
    elif t == "precedent_marked":
        s.precedent[e["decision_id"]] = {"by": e["actor"], "ts": e["ts"], "note": e.get("note")}
    elif t == "precedent_unmarked":
        s.precedent.pop(e["decision_id"], None)
    elif t == "task_created":
        s.tasks[e["task_id"]] = {**{k: e.get(k) for k in ("task_id", "kind", "subject", "prompt_path", "provider",
                                                          "template")},
                                 "created_by": e["actor"], "created_at": e["ts"], "status": "waiting_response",
                                 "result": None, "viewed_by": []}
    elif t == "task_dispatched":
        if e["task_id"] in s.tasks:
            s.tasks[e["task_id"]]["dispatched"] = {"provider": e.get("provider"), "ts": e["ts"]}
    elif t == "task_response_received":
        tk = s.tasks.get(e["task_id"])
        if tk:
            tk.update(status=e.get("status", "done"), result=e.get("result"), response_blob=e.get("blob"),
                      answered_at=e["ts"], issues=e.get("issues"))
    elif t == "task_viewed":
        tk = s.tasks.get(e["task_id"])
        if tk:
            tk["viewed_by"].append({"actor": e["actor"], "ts": e["ts"]})
    elif t == "proposal_created":
        s.proposals[e["proposal_id"]] = {**{k: v for k, v in e.items() if k not in ("prev", "hash", "seq", "id")},
                                         "created_by": e["actor"], "created_at": e["ts"], "status": "open"}
    elif t in ("proposal_accepted", "proposal_rejected"):
        p = s.proposals.get(e["proposal_id"])
        if p:
            p.update(status="accepted" if t == "proposal_accepted" else "rejected", resolved_by=e["actor"],
                     resolved_at=e["ts"], resolution=e.get("reason"), resulting_version=e.get("resulting_version"),
                     edited=bool(e.get("edited_ops")))
    elif t == "regression_run":
        s.regressions[e["regression_id"]] = {k: v for k, v in e.items() if k not in ("prev", "hash")}
    elif t == "metrics_computed":
        s.metric_reports[e["report_id"]] = {k: v for k, v in e.items() if k not in ("prev", "hash")}
    elif t == "gt_version_created":
        s.gt_versions.append({k: v for k, v in e.items() if k not in ("prev", "hash")})
    elif t == "digest_built":
        s.digests.append({k: v for k, v in e.items() if k not in ("prev", "hash")})
    elif t in ("checklist_version_created", "version_activated", "version_archived", "version_restored"):
        s.checklist_events.append(e)
    elif t in ("config_version_registered", "config_changed"):
        s.config_versions.append(e)
    elif t == "dataset_imported":
        s.datasets.append(e)


_CACHE: dict = {}


def load(until: Optional[str] = None) -> State:
    key = (events.fingerprint(), until)
    if key in _CACHE:
        return _CACHE[key]
    s = State()
    for e in events.read(until=until):
        s.events.append(e)
        _apply(s, e)
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = s
    return s


# --- the history feed (H.7) ---------------------------------------------------------------

def feed_row(e: dict, s: Optional[State] = None) -> dict:
    """One line of the history table: when, event, conversation, criterion, who, before → after, reason, links."""
    t = e["type"]
    row = {"seq": e.get("seq"), "id": e.get("id"), "ts": e["ts"], "type": t, "actor": e.get("actor"),
           "conversation_id": e.get("conversation_id"), "criterion_id": e.get("criterion_id"),
           "before": None, "after": None, "reason": e.get("reason") or e.get("comment"), "links": {}}
    if t == "evaluation_imported":
        row.update(before=None, after=e.get("total"), reason=f"{e.get('agent')} · {e.get('checklist')}",
                   links={"evaluation_id": e["evaluation_id"], "run_id": e.get("run_id"), "blob": e.get("blob")})
    elif t == "decision_made":
        row.update(before=e.get("old_answer"), after=e.get("new_answer"),
                   reason=f"[{e.get('appeal_type')}] {e.get('reason', '')}",
                   links={"decision_id": e["decision_id"], "appeal_id": e.get("appeal_id"),
                          "evaluation_id": e.get("evaluation_id")})
    elif t == "appeal_filed":
        row["links"] = {"appeal_id": e["appeal_id"], "evaluation_id": e.get("evaluation_id")}
    elif t in ("checklist_version_created",):
        row.update(before=e.get("parent"), after=e.get("checklist"), links={"source": e.get("source")})
    elif t == "version_activated":
        row.update(after=e.get("ref"), links={"regression_id": e.get("regression_id")})
    elif t == "proposal_created":
        row.update(criterion_id=e.get("criterion_id"), reason=e.get("title"),
                   links={"proposal_id": e["proposal_id"], "task_id": e.get("task_id")})
    elif t in ("proposal_accepted", "proposal_rejected"):
        row.update(after=e.get("resulting_version"), links={"proposal_id": e["proposal_id"]})
    elif t == "config_changed":
        row.update(before=e.get("old"), after=e.get("new"), criterion_id=None)
    return row
