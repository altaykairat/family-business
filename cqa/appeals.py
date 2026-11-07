"""Contour 2: appeals, reviewer decisions, precedents (decisions 4, 7, 11; AP.*, HU.*, HX.5–HX.6).

- An appeal targets one criterion of one evaluation. The reviewer confirms or changes the answer, gives a
  reason and the type A–D. The original evaluation never changes: the final result is recomputed on top.
- Type and action must agree: A — the answer changes; B — reviewer's call; C and D — the answer stays.
- A decision is a precedent only after the button «Использовать как прецедент» (decision 11).
"""

from __future__ import annotations

import re
from typing import Optional

from . import access, checklist as ck, common, events, state
from . import conversation as cv
from . import evaluations as evs
from . import responses as rs
from .common import CQAError, parse_iso


def _next_id(prefix: str, existing) -> str:
    return f"{prefix}-{len(existing) + 1:04d}"


def _sim(simulated: Optional[dict]) -> dict:
    """Simulated actions (sim.appeals) carry the batch tag in every event; real ones carry nothing extra."""
    return {"simulated": simulated} if simulated else {}


def file(evaluation_id: str, criterion_id: str, comment: str, actor: str, operator_id: Optional[str] = None,
         turn_idx: Optional[int] = None, attachments: Optional[list] = None, simulated: Optional[dict] = None) -> dict:
    st = state.load()
    role = access.require(actor, "appeal.file", st)
    ev = st.evaluation(evaluation_id)
    doc = st.doc(ev["checklist"])
    ck.criterion(doc, criterion_id)
    if not (comment or "").strip():
        raise CQAError("an appeal needs a comment")
    operator_id = operator_id or (actor if role == "operator" else st.operator_of.get(ev["conversation_id"]))
    open_same = [a for a in st.appeals.values() if a["evaluation_id"] == evaluation_id
                 and a["criterion_id"] == criterion_id and a["status"] in ("open", "assigned")]
    if open_same:
        raise CQAError(f"appeal {open_same[0]['appeal_id']} on this criterion is still open", "conflict")
    stored = []
    for att in attachments or []:
        content = att.get("content", "")
        h = events.put_blob({"name": att.get("name"), "content": content})
        stored.append({"name": att.get("name"), "blob": h})
    appeal_id = _next_id("ap", st.appeals)
    events.append("appeal_filed", actor=actor, appeal_id=appeal_id, evaluation_id=evaluation_id,
                  conversation_id=ev["conversation_id"], criterion_id=criterion_id, operator_id=operator_id,
                  comment=comment, turn_idx=turn_idx, attachments=stored,
                  disputed_answer=st.final(evaluation_id)["final"]["answers"].get(criterion_id), **_sim(simulated))
    return {"appeal_id": appeal_id}


def assign(appeal_id: str, reviewer_id: str, actor: str, simulated: Optional[dict] = None) -> dict:
    st = state.load()
    access.require(actor, "appeal.assign", st)
    ap = st.appeal(appeal_id)
    if ap["status"] in ("decided", "closed"):
        raise CQAError(f"appeal {appeal_id} is already {ap['status']}", "conflict")
    if st.role_of(reviewer_id) not in ("reviewer", "admin"):
        raise CQAError(f"{reviewer_id} is not a reviewer")
    events.append("appeal_assigned", actor=actor, appeal_id=appeal_id, reviewer_id=reviewer_id,
                  conversation_id=ap["conversation_id"], criterion_id=ap["criterion_id"], **_sim(simulated))
    return {"appeal_id": appeal_id, "assigned_to": reviewer_id}


def view(appeal_id: str, actor: str, reveal: bool = False) -> dict:
    """What the reviewer sees: disputed criterion (text of the evaluation's checklist version, VC.8), the
    operator's comment, the dialog fragment, the model's answer and reasoning (hidden in blind mode until
    `reveal`, HU.5), counter-argument tasks."""
    st = state.load()
    access.require(actor, "appeal.open", st)
    ap = st.appeal(appeal_id)
    if not ap["opened_at"]:
        events.append("appeal_opened", actor=actor, appeal_id=appeal_id, conversation_id=ap["conversation_id"],
                      criterion_id=ap["criterion_id"])
    blind = bool(st.reviewer_settings.get(actor, {}).get("blind"))
    already = any(r["reviewer_id"] == actor and r.get("appeal_id") == appeal_id for r in st.reveals)
    if blind and reveal and not already:
        events.append("model_answer_revealed", actor=actor, appeal_id=appeal_id,
                      evaluation_id=ap["evaluation_id"], criterion_id=ap["criterion_id"],
                      conversation_id=ap["conversation_id"])
        already = True
    ev = st.evaluation(ap["evaluation_id"])
    doc = st.doc(ev["checklist"])
    c = ck.criterion(doc, ap["criterion_id"])
    data = evs.details(ap["evaluation_id"], st)
    item = (data.get("criteria") or {}).get(ap["criterion_id"], {})
    conv = cv.load(ev.get("dataset") or "dev", ap["conversation_id"])
    focus = {e.get("idx") for e in item.get("evidence") or []} | ({ap["turn_idx"]} if ap.get("turn_idx") else set())
    hide_model = blind and not already
    return {
        "appeal": ap,
        "criterion": c,
        "checklist": ev["checklist"],
        "global_rules": doc.get("global_rules", []),
        "fragment": fragment(conv, focus),
        "transcript_size": len(conv["transcript"]),
        "model": None if hide_model else {
            "answer": ev["answers"].get(ap["criterion_id"]), "explanation": item.get("explanation"),
            "reasoning": item.get("reasoning"), "evidence": item.get("evidence"),
            "evidence_checks": item.get("evidence_checks"), "considered_evidence": item.get("considered_evidence"),
            "checked_range": item.get("checked_range"), "confidence": item.get("confidence")},
        "current_final_answer": st.final(ap["evaluation_id"])["final"]["answers"].get(ap["criterion_id"]),
        "blind_mode": blind, "model_hidden": hide_model,
        "counterarguments": [{"task_id": t["task_id"], "status": t["status"], "result": t["result"]}
                             for t in st.tasks.values() if t["kind"] == "counterargument"
                             and (t.get("subject") or {}).get("appeal_id") == appeal_id],
        "type_suggestions": [{"task_id": t["task_id"], "result": t["result"]} for t in st.tasks.values()
                             if t["kind"] == "suggest_appeal_type"
                             and (t.get("subject") or {}).get("appeal_id") == appeal_id],
        "appeal_types": state.APPEAL_TYPES,
    }


def fragment(conv: dict, focus: set, radius: int = 2) -> list:
    if not focus:
        return [_turn(t) for t in conv["transcript"][-6:]]
    keep = set()
    for i in focus:
        if isinstance(i, int):
            keep.update(range(i - radius, i + radius + 1))
    return [_turn(t) | {"focus": t["idx"] in focus} for t in conv["transcript"] if t["idx"] in keep]


def _turn(t: dict) -> dict:
    return {"idx": t["idx"], "timestamp": t["timestamp"], "offset_s": t["t"], "speaker": t["speaker"],
            "text": t["text"]}


def decide(actor: str, action: str, reason: str, appeal_type: str, appeal_id: Optional[str] = None,
           evaluation_id: Optional[str] = None, criterion_id: Optional[str] = None, new_answer=None,
           overall_score: Optional[float] = None, time_spent_s: Optional[float] = None,
           revise: bool = False, simulated: Optional[dict] = None) -> dict:
    """Reviewer decision — ground truth (decision 7). Without appeal_id it is an audit decision (AP.7)."""
    st = state.load()
    access.require(actor, "decision.make", st)
    if appeal_id:
        ap = st.appeal(appeal_id)
        if ap["status"] == "closed":
            raise CQAError(f"appeal {appeal_id} is closed", "conflict")
        if ap["status"] == "decided" and not revise:
            raise CQAError(f"appeal {appeal_id} is already decided ({ap['decision_id']}); pass revise=true "
                           "to record a new decision on top", "conflict")
        evaluation_id, criterion_id = ap["evaluation_id"], ap["criterion_id"]
    elif not (evaluation_id and criterion_id):
        raise CQAError("give appeal_id, or evaluation_id + criterion_id for an audit decision")
    ev = st.evaluation(evaluation_id)
    doc = st.doc(ev["checklist"])
    c = ck.criterion(doc, criterion_id)
    appeal_type = (appeal_type or "").upper()
    if appeal_type not in state.APPEAL_TYPES:
        raise CQAError(f"type must be one of {sorted(state.APPEAL_TYPES)}")
    if action not in ("confirm", "change"):
        raise CQAError("action must be confirm or change")
    if not (reason or "").strip():
        raise CQAError("a decision needs a reason")
    old = st.final(evaluation_id)["final"]["answers"].get(criterion_id)
    if action == "change":
        norm, problem = rs.normalize_answer(new_answer, c)
        if norm is None or (problem and "округлено" in problem):
            raise CQAError(f"invalid new answer {new_answer!r} for {criterion_id}: {problem}")
        if str(norm) == str(old):
            raise CQAError("new answer equals the current one — use confirm")
        new = norm
    else:
        new = old
    if appeal_type == "A" and action != "change":
        raise CQAError("type A (ошибка оценки) means the answer changes — use action change")
    if appeal_type in ("C", "D") and action != "confirm":
        raise CQAError(f"type {appeal_type} keeps the answer (регламент действует) — use action confirm")
    now = common.now_iso()
    start = None
    if appeal_id:
        start = st.appeals[appeal_id].get("opened_at") or st.appeals[appeal_id].get("assigned_at")
    if time_spent_s is None and start:
        time_spent_s = round((parse_iso(now) - parse_iso(start)).total_seconds(), 1)
    saw_counter = any(t["kind"] == "counterargument" and (t.get("subject") or {}).get("appeal_id") == appeal_id
                      and any(v["actor"] == actor for v in t["viewed_by"]) for t in st.tasks.values()) if appeal_id else False
    previous = st.decisions_for(evaluation_id).get(criterion_id)
    decision_id = f"d-{len(st.decisions) + 1:04d}"
    points_delta = _points_delta(doc, ev, st, criterion_id, new)
    events.append("decision_made", actor=actor, decision_id=decision_id, appeal_id=appeal_id,
                  evaluation_id=evaluation_id, conversation_id=ev["conversation_id"], criterion_id=criterion_id,
                  criterion_rev=ck.criterion_revs(doc).get(criterion_id), reviewer_id=actor, action=action,
                  model_answer=ev["answers"].get(criterion_id), old_answer=old, new_answer=new, reason=reason,
                  appeal_type=appeal_type, judged_on=ev["checklist"], operator_id=st.operator_of.get(ev["conversation_id"]),
                  saw_counterargument=saw_counter,
                  blind=bool(st.reviewer_settings.get(actor, {}).get("blind")),
                  revealed=any(r["reviewer_id"] == actor and r.get("appeal_id") == appeal_id for r in st.reveals),
                  time_spent_s=time_spent_s, overall_score=overall_score, supersedes=previous and previous["decision_id"],
                  total_delta=points_delta, agent=ev["agent"], run_id=ev["run_id"], **_sim(simulated))
    fin = state.load().final(evaluation_id)
    return {"decision_id": decision_id, "old_answer": old, "new_answer": new,
            "original_total": fin["original"]["total"], "final_total": fin["final"]["total"],
            "gross_error": fin["final"]["gross_error"]}


def _points_delta(doc, ev, st, criterion_id, new_answer):
    from . import scoring

    current = st.final(ev["evaluation_id"])["final"]["answers"]
    before = scoring.score(doc, current)["total"]
    after = scoring.score(doc, {**current, criterion_id: new_answer})["total"]
    return None if before is None or after is None else round(after - before, 2)


def close(appeal_id: str, reason: str, actor: str) -> dict:
    st = state.load()
    access.require(actor, "appeal.close", st)
    ap = st.appeal(appeal_id)
    if ap["status"] in ("decided", "closed"):
        raise CQAError(f"appeal {appeal_id} is already {ap['status']}", "conflict")
    events.append("appeal_closed", actor=actor, appeal_id=appeal_id, reason=reason,
                  conversation_id=ap["conversation_id"], criterion_id=ap["criterion_id"])
    return {"appeal_id": appeal_id, "status": "closed"}


def set_reviewer_settings(reviewer_id: str, actor: str, blind: Optional[bool] = None) -> dict:
    st = state.load()
    access.require(actor, "reviewer.settings", st)
    settings = {}
    if blind is not None:
        settings["blind"] = bool(blind)
    events.append("reviewer_settings_changed", actor=actor, reviewer_id=reviewer_id, settings=settings)
    return {"reviewer_id": reviewer_id, **settings}


def list_appeals(status: Optional[str] = None, reviewer: Optional[str] = None, operator: Optional[str] = None,
                 until: Optional[str] = None) -> list:
    st = state.load(until)
    out = []
    for ap in st.appeals.values():
        if status and ap["status"] != status:
            continue
        if reviewer and ap.get("assigned_to") != reviewer:
            continue
        if operator and ap.get("operator_id") != operator:
            continue
        d = st.decisions.get(ap["decision_id"]) if ap.get("decision_id") else None
        out.append({**ap, "decision": d and {k: d.get(k) for k in ("decision_id", "action", "new_answer", "type",
                                                                   "reason", "reviewer_id")}})
    return out


# --- precedents (mechanism A) -----------------------------------------------------------------

def mark_precedent(decision_id: str, actor: str, note: str = "", on: bool = True) -> dict:
    st = state.load()
    access.require(actor, "precedent.mark", st)
    if decision_id not in st.decisions:
        raise CQAError(f"decision {decision_id} not found", "not_found")
    if on == (decision_id in st.precedent):
        raise CQAError(f"decision {decision_id} is {'already' if on else 'not'} a precedent", "conflict")
    d = st.decisions[decision_id]
    events.append("precedent_marked" if on else "precedent_unmarked", actor=actor, decision_id=decision_id,
                  note=note, conversation_id=d["conversation_id"], criterion_id=d["criterion_id"])
    return {"decision_id": decision_id, "precedent": on}


def precedents(criterion_id: Optional[str] = None, until: Optional[str] = None) -> list:
    st = state.load(until)
    out = []
    for d in st.active_precedents():
        if criterion_id and d["criterion_id"] != criterion_id:
            continue
        out.append(precedent_card(st, d))
    return out


def precedent_card(st: state.State, d: dict) -> dict:
    ev = st.evaluations.get(d["evaluation_id"], {})
    item = {}
    try:
        item = (evs.details(d["evaluation_id"], st).get("criteria") or {}).get(d["criterion_id"], {})
    except CQAError:
        pass
    conv = cv.load(ev.get("dataset") or "dev", d["conversation_id"])
    ap = st.appeals.get(d.get("appeal_id") or "", {})
    focus = {e.get("idx") for e in item.get("evidence") or []} | ({ap["turn_idx"]} if ap.get("turn_idx") else set())
    return {"decision_id": d["decision_id"], "criterion_id": d["criterion_id"], "criterion_rev": d.get("criterion_rev"),
            "conversation_id": d["conversation_id"], "model_answer": d.get("model_answer"),
            "reviewer_answer": d["new_answer"], "type": d["type"], "reason": d["reason"],
            "judged_on": d.get("judged_on"), "fragment": fragment(conv, focus, radius=1),
            "marked": st.precedent.get(d["decision_id"])}


def _words(text: str) -> set:
    return {w for w in re.findall(r"\w{4,}", text.lower())}


def select_precedents(conv: dict, criterion_ids: list, k: Optional[int] = None) -> list:
    """CL.A: same criterion + keyword overlap with the conversation, top-k per criterion."""
    k = k if k is not None else int(common.setting("precedents.k", 3))
    st = state.load()
    words = _words(" ".join(t["text"] for t in conv["transcript"]))
    picked = []
    for cid in criterion_ids:
        cands = [precedent_card(st, d) for d in st.active_precedents()
                 if d["criterion_id"] == cid and d["conversation_id"] != conv["conversation_id"]]
        cands.sort(key=lambda p: -len(words & _words(" ".join(t["text"] for t in p["fragment"]))))
        picked += cands[:k]
    return picked


def render_precedents(items: list) -> str:
    """HX.6: «модель: X → проверяющий: Y, причина: Z» + fragment."""
    if not items:
        return "(прецедентов нет)"
    lines = []
    for p in items:
        frag = "\n".join(f"    #{t['idx']} {t['speaker']}: {' '.join(t['text'].split())}" for t in p["fragment"])
        lines.append(f"- {p['criterion_id']} · модель: «{p['model_answer']}» → проверяющий: «{p['reviewer_answer']}» "
                     f"(тип {p['type']}). Причина: {p['reason']}\n{frag}")
    return "\n".join(lines)


def conflicts(until: Optional[str] = None) -> list:
    """HU.6: different reviewers decided the same conversation × criterion differently. The system shows the
    conflict to an admin; it never picks a winner."""
    st = state.load(until)
    groups: dict = {}
    for d in st.decisions.values():
        groups.setdefault((d["conversation_id"], d["criterion_id"]), []).append(d)
    out = []
    for (conv, crit), ds in groups.items():
        answers = {str(d["new_answer"]) for d in ds}
        reviewers = {d["reviewer_id"] for d in ds}
        if len(answers) > 1 and len(reviewers) > 1:
            out.append({"conversation_id": conv, "criterion_id": crit,
                        "decisions": [{k: d.get(k) for k in ("decision_id", "reviewer_id", "new_answer", "type",
                                                             "reason", "decided_at")} for d in ds]})
    return out
