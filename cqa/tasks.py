"""Orchestrator LLM steps as files (S10.4, HU.3, AP.5, K.7). The platform builds the prompt by an algorithm,
a provider (now: a human) produces the response, the platform parses it into *proposals* or *hints*.
Nothing an orchestrator returns is applied: proposals wait for a human «Принять» / «Отклонить».

history/tasks/<task_id>/prompt.md   — exactly what the model sees
history/tasks/<task_id>/response.md — the pasted answer (or written by a CLI provider)

Kinds:
  analyze_history      digest of the history → proposals (rules, notes, N/A conditions, wording, weights…)
  checklist_edit       a request in plain words → structured edit operations (a proposal)
  counterargument      the model's arguments against a reviewer's view of an appeal (shown, never decides)
  suggest_appeal_type  a hint which type A–D fits the reviewer's reason (the reviewer confirms)

Templates: prompts/templates/task_<kind>.md if the design session provides one, else
config/task_templates/<kind>.md. Placeholders {{NAME}} are filled from the task subject.
"""

from __future__ import annotations

import json
from typing import Optional

from . import access, checklist as ck
from . import common, events, providers, state, versions
from . import responses as rs
from .common import CQAError, load_json, save_json

KINDS = ("analyze_history", "checklist_edit", "counterargument", "suggest_appeal_type")


def template(kind: str) -> tuple:
    for path in (common.TEMPLATES / f"task_{kind}.md", common.TASK_TEMPLATES / f"{kind}.md"):
        if path.exists():
            return path.read_text(encoding="utf-8"), path
    raise CQAError(f"no template for task {kind}", "not_found")


def _fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
    return text


def _checklist_brief(doc: dict) -> dict:
    return {"ref": ck.ref_of(doc), "sections": doc.get("sections"), "global_rules": doc.get("global_rules"),
            "criteria": [{k: c.get(k) for k in ("id", "title", "section", "type", "weight", "description",
                                                  "na_allowed", "na_condition", "scale", "notes", "status")}
                         for c in doc["criteria"]]}


def create(kind: str, actor: str, subject: dict, provider: Optional[str] = None) -> dict:
    st = state.load()
    access.require(actor, "task.create", st)
    if kind not in KINDS:
        raise CQAError(f"unknown task kind {kind!r}, expected {KINDS}")
    values = {}
    subject = dict(subject or {})
    if kind == "analyze_history":
        from . import loop

        digest = loop.load_digest(subject.get("digest_id")) if subject.get("digest_id") else loop.build_digest(actor)
        subject["digest_id"] = digest["digest_id"]
        doc = ck.load(digest["checklist"])
        subject["base"] = ck.ref_of(doc)
        values = {"DIGEST": digest, "CHECKLIST": _checklist_brief(doc), "OPS": ck.apply_ops.__doc__,
                  "APPEAL_TYPES": state.APPEAL_TYPES}
    elif kind == "checklist_edit":
        if not subject.get("request"):
            raise CQAError("checklist_edit needs subject.request — the change in plain words")
        doc = ck.load(subject.get("base") or "default")
        subject["base"] = ck.ref_of(doc)
        values = {"REQUEST": subject["request"], "CHECKLIST": _checklist_brief(doc), "OPS": ck.apply_ops.__doc__}
    else:
        ap_id = subject.get("appeal_id")
        if not ap_id:
            raise CQAError(f"{kind} needs subject.appeal_id")
        ctx = _appeal_context(st, ap_id)
        values = {"APPEAL": ctx, "REVIEWER_REASON": subject.get("reviewer_reason") or "(не указана)",
                  "APPEAL_TYPES": state.APPEAL_TYPES}
    text, tpath = template(kind)
    tv = versions.register("task_template", kind, text, actor, "used by a task")
    task_id = f"t-{len(st.tasks) + 1:04d}"
    folder = common.TASKS / task_id
    folder.mkdir(parents=True, exist_ok=True)
    prompt = _fill(text, values)
    (folder / "prompt.md").write_text(prompt, encoding="utf-8")
    prov = providers.get(provider, agent="orchestrator")
    meta = {"task_id": task_id, "kind": kind, "subject": subject, "provider": prov.name,
            "template": {"name": kind, "version": tv["version"], "hash": tv["hash"], "path": common.rel(tpath)}}
    save_json(folder / "task.json", meta)
    events.append("task_created", actor=actor, task_id=task_id, kind=kind, subject=subject,
                  prompt_path=common.rel(folder / "prompt.md"), provider=prov.name, template=meta["template"],
                  conversation_id=subject.get("conversation_id"), criterion_id=subject.get("criterion_id"))
    return {"task_id": task_id, "prompt_path": common.rel(folder / "prompt.md"),
            "response_path": common.rel(folder / "response.md"), "provider": prov.describe()}


def _appeal_context(st: state.State, appeal_id: str) -> dict:
    from . import conversation as cv
    from . import evaluations as evs

    ap = st.appeal(appeal_id)
    ev = st.evaluation(ap["evaluation_id"])
    doc = st.doc(ev["checklist"])
    item = (evs.details(ap["evaluation_id"], st).get("criteria") or {}).get(ap["criterion_id"], {})
    conv = cv.load(ev.get("dataset") or "dev", ap["conversation_id"])
    return {"criterion": ck.criterion(doc, ap["criterion_id"]), "global_rules": doc.get("global_rules"),
            "operator_comment": ap["comment"], "model_answer": ev["answers"].get(ap["criterion_id"]),
            "model_explanation": item.get("explanation"), "model_evidence": item.get("evidence"),
            "dialog": [{"idx": t["idx"], "speaker": t["speaker"], "text": t["text"]} for t in conv["transcript"]]}


def prompt(task_id: str) -> dict:
    st = state.load()
    t = _task(st, task_id)
    return {"task_id": task_id, "kind": t["kind"],
            "text": (common.WORKSPACE / t["prompt_path"]).read_text(encoding="utf-8")}


def _task(st: state.State, task_id: str) -> dict:
    t = st.tasks.get(task_id)
    if t is None:
        raise CQAError(f"task {task_id} not found", "not_found")
    return t


def submit(task_id: str, text: str, actor: str) -> dict:
    """Save a pasted response (what the frontend does instead of writing the file by hand)."""
    st = state.load()
    access.require(actor, "task.respond", st)
    _task(st, task_id)
    if not (text or "").strip():
        raise CQAError("empty response")
    (common.TASKS / task_id / "response.md").write_text(text, encoding="utf-8")
    return ingest(task_id, actor)


def dispatch(task_id: str, actor: str) -> dict:
    st = state.load()
    access.require(actor, "task.create", st)
    t = _task(st, task_id)
    prov = providers.get(t.get("provider"))
    folder = common.TASKS / task_id
    res = prov.complete(folder / "prompt.md", folder / "response.md")
    events.append("task_dispatched", actor=actor, task_id=task_id, provider=prov.name, result=res.get("status"))
    if res.get("status") == "done" and t["status"] == "waiting_response":
        return {**res, "ingest": ingest(task_id, actor)}
    return res


def ingest(task_id: str, actor: str) -> dict:
    st = state.load()
    t = _task(st, task_id)
    path = common.TASKS / task_id / "response.md"
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise CQAError(f"no response yet: paste it into {common.rel(path)}", "not_found")
    raw = path.read_text(encoding="utf-8")
    data, err = rs.parse(raw)
    blob = events.put_blob({"task_id": task_id, "raw": raw})
    if err:
        events.append("task_response_received", actor=actor, task_id=task_id, status="invalid", issues=[err],
                      blob=blob, result=None)
        raise CQAError(f"response of {task_id} is not valid JSON: {err}")
    issues, result = [], {}
    if t["kind"] in ("analyze_history", "checklist_edit"):
        from . import loop

        items = data.get("proposals") if t["kind"] == "analyze_history" else [
            {"kind": "checklist_edit", "title": data.get("summary") or t["subject"].get("request"),
             "rationale": data.get("rationale") or t["subject"].get("request"), "ops": data.get("ops") or [],
             "criterion_id": data.get("criterion_id")}]
        created = []
        for p in items or []:
            res = loop.create_proposal(actor="orchestrator", source="orchestrator", task_id=task_id,
                                       base=t["subject"].get("base"), **{k: p.get(k) for k in (
                                           "kind", "title", "rationale", "ops", "criterion_id",
                                           "evidence_decisions", "expected_effect")})
            created.append(res["proposal_id"])
        result = {"proposals": created, "conflicts": data.get("conflicts"), "notes": data.get("notes")}
    elif t["kind"] == "counterargument":
        result = {"counterarguments": data.get("counterarguments") or [], "summary": data.get("summary")}
    elif t["kind"] == "suggest_appeal_type":
        typ = str(data.get("type", "")).upper()
        if typ not in state.APPEAL_TYPES:
            issues.append(f"unknown type {typ!r}")
        result = {"type": typ, "rationale": data.get("rationale")}
    events.append("task_response_received", actor=actor, task_id=task_id, status="done", issues=issues,
                  blob=blob, result=result, conversation_id=t["subject"].get("conversation_id"))
    return {"task_id": task_id, "result": result, "issues": issues}


def view(task_id: str, actor: str) -> dict:
    """Show a task result; viewing a counter-argument is recorded (HU.4: did the reviewer see it?)."""
    st = state.load()
    t = _task(st, task_id)
    if t["status"] == "done":
        events.append("task_viewed", actor=actor, task_id=task_id)
    return t


def listing(kind: Optional[str] = None, status: Optional[str] = None, until: Optional[str] = None) -> list:
    st = state.load(until)
    return [t for t in st.tasks.values() if (kind is None or t["kind"] == kind)
            and (status is None or t["status"] == status)]
