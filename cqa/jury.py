"""Jury scenarios end to end (sprint 1, items 6–7) and full traceability.

  gross_errors  list of dialogs with a gross error and the explanation of the flag criterion (scenario 1)
  rescore       a new checklist version → recompute published evaluations without LLM: unchanged criteria keep
                their answers, weights/N-A redistribution follow the new version; criteria whose wording changed
                or that are new are taken from `fill_from` (a run prepared with `criteria=` only for them) or are
                reported as needing an LLM call (scenario 3)
  trace         any answer → exact prompt text, raw response, versions (checklist, template, params, rule
                clarifications, precedents) and every human action on the conversation (item 7)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import access, checklist as ck
from . import common, events, scoring, sources, state
from . import conversation as cv
from .common import CQAError, rel, stable_hash, text_hash


def text_hash_of(path: Path) -> str:
    return text_hash(path.read_text(encoding="utf-8"))


def _flag_ids(doc: dict) -> list:
    return [c["id"] for c in ck.active_criteria(doc) if c["type"] == "flag"]


def gross_errors(source: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Dialogs whose final result has the gross-error flag, with the flag's explanation and evidence turns."""
    st = state.load(until)
    if source:
        src = sources.resolve(source)
        eids = [eid for (r, a, _), eid in st.latest_by_key.items() if (r, a) == (src["run_id"], src["agent"])]
    else:
        eids = list(st.published.values())
    rows = []
    for eid in eids:
        ev = st.evaluations[eid]
        fin = st.final(eid)
        if not fin["final"]["gross_error"]:
            continue
        doc = st.doc(ev["checklist"])
        data = events.get_blob(ev["blob"]) if ev.get("blob") else {}
        conv = cv.load(ev.get("dataset") or "dev", ev["conversation_id"])
        turns = {t["idx"]: t for t in conv["transcript"]}
        for fid in _flag_ids(doc):
            if fin["final"]["answers"].get(fid) != "yes":
                continue
            item = (data.get("criteria") or {}).get(fid) or {}
            dec = st.decisions_for(eid).get(fid)
            rows.append({
                "conversation_id": ev["conversation_id"], "evaluation_id": eid, "criterion_id": fid,
                "channel": conv["channel"], "language": conv["language"],
                "operator_id": st.operator_of.get(ev["conversation_id"]), "total": fin["final"]["total"],
                "explanation": (f"Решение проверяющего: {dec['reason']}" if dec and dec["action"] == "change"
                                else item.get("explanation")),
                "evidence": [{"idx": e.get("idx"), "quote": e.get("quote"),
                              "speaker": (turns.get(e.get("idx")) or {}).get("speaker"),
                              "text": (turns.get(e.get("idx")) or {}).get("text")} for e in item.get("evidence") or []],
                "decided_by_human": bool(dec), "source": f"{ev['run_id']}:{ev['agent']}"})
    rows.sort(key=lambda r: (r["total"] if r["total"] is not None else 999, r["conversation_id"]))
    return {"source": source or "published", "count": len(rows), "of": len(eids), "items": rows}


def rescore(ref: str, actor: str, fill_from: Optional[str] = None, publish: bool = False,
            conversation_ids: Optional[list] = None) -> dict:
    """Carry published evaluations over to checklist version `ref` (normally a fresh draft or the new HEAD)."""
    from . import evaluations

    st = state.load()
    access.require(actor, "evaluation.run", st)
    new_doc = ck.load(ref)
    new_ref = ck.ref_of(new_doc)
    new_revs = ck.criterion_revs(new_doc)
    fill = sources.load_results(fill_from)[1] if fill_from else {}
    created, incomplete = [], []
    for conv_id, eid in sorted(st.published.items()):
        if conversation_ids and conv_id not in conversation_ids:
            continue
        ev = st.evaluations[eid]
        if ev["checklist"] == new_ref:
            continue
        old_revs = (ev.get("versions") or {}).get("checklist", {}).get("criteria_revs") or \
            ck.criterion_revs(st.doc(ev["checklist"]))
        fin = st.final(eid)["final"]["answers"]
        blob = events.get_blob(ev["blob"]) if ev.get("blob") else {}
        answers, criteria, origin, need = {}, {}, {}, []
        filled = fill.get(conv_id) or {}
        for c in ck.active_criteria(new_doc):
            cid = c["id"]
            if cid in (filled.get("answers") or {}):
                answers[cid] = filled["answers"][cid]
                criteria[cid] = (filled.get("criteria") or {}).get(cid) or {}
                origin[cid] = f"llm:{fill_from}"
            elif old_revs.get(cid) == new_revs.get(cid) and cid in fin:
                answers[cid] = fin[cid]
                criteria[cid] = (blob.get("criteria") or {}).get(cid) or {}
                origin[cid] = "carried" if fin[cid] == ev["answers"].get(cid) else "carried:reviewer"
            else:
                need.append(cid)
        sc = scoring.score(new_doc, answers)
        data = {"run_id": f"rescore-{new_ref}", "agent": ev["agent"], "conversation_id": conv_id,
                "checklist": new_ref, "variant": "rescore", "status": "warn" if need else "ok",
                "answers": answers, "criteria": criteria, "score": sc, "dialog_facts": blob.get("dialog_facts"),
                "issues": [{"level": "warn", "criterion_id": c, "msg": "нужен вызов LLM: критерий новый или "
                            "изменён по смыслу"} for c in need],
                "rescored_from": eid, "answer_origin": origin, "usage": filled.get("usage") or []}
        new_id = f"rescore-{new_ref}:{ev['agent']}:{conv_id}:{stable_hash(data, 8)}"
        if new_id in st.evaluations:
            continue
        events.append("evaluation_imported", actor=actor, evaluation_id=new_id, run_id=data["run_id"],
                      agent=ev["agent"], conversation_id=conv_id, dataset=ev.get("dataset"), checklist=new_ref,
                      variant="rescore", status=data["status"], answers=answers, total=sc["total"],
                      gross_error=sc["gross_error"], complete=sc["complete"], blob=events.put_blob(data),
                      source=f"rescore:{eid}", supersedes=None, usage=evaluations._usage_summary(data),
                      versions={**(ev.get("versions") or {}),
                                "checklist": {"ref": new_ref, "hash": new_doc["meta"]["content_hash"],
                                              "criteria_revs": new_revs,
                                              "clarification_revs": ck.clarification_revs(new_doc)},
                                "rescored_from": eid, "answer_origin": origin})
        created.append({"conversation_id": conv_id, "evaluation_id": new_id, "old_total": st.final(eid)["final"]["total"],
                        "new_total": sc["total"], "needs_llm": need})
        if need:
            incomplete.append(conv_id)
    if publish:
        ready = [c["evaluation_id"] for c in created if not c["needs_llm"]]
        if ready:
            evaluations.publish(ready, actor)
    need_crit = sorted({c for r in created for c in r["needs_llm"]})
    return {"checklist": new_ref, "rescored": len(created), "published": bool(publish),
            "needs_llm": {"conversations": len(incomplete), "criteria": need_crit,
                          "how": (f"run.prepare checklist={new_ref} criteria={','.join(need_crit)} → вставки → "
                                  f"run.ingest → jury.rescore ref={new_ref} fill_from=<run>:<agent>")
                          if need_crit else None},
            "items": created}


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (common.ROOT / p if (common.ROOT / p).exists() else common.WORKSPACE / p)


def _prompt_files(ev: dict) -> list:
    """Exact prompt and raw response files behind an evaluation (platform runs and variant runs)."""
    out = []
    run_dir = common.RUNS / ev["run_id"]
    if not run_dir.exists():
        hits = sorted(common.ROOT.glob(f"variants/*/runs/{ev['run_id']}"))
        run_dir = hits[0] if hits else run_dir
    data = events.get_blob(ev["blob"]) if ev.get("blob") else {}
    usage = {u.get("part"): u for u in data.get("usage") or []}
    for p in (ev.get("versions") or {}).get("prompts") or []:
        name = Path(p.get("file") or "").stem
        prompt = run_dir / "agents" / ev["agent"] / "prompts" / f"{name}.md"
        if not prompt.exists():
            prompt = run_dir / "prompts" / f"{name}.md"
        resp = (usage.get(p.get("part")) or {}).get("response_file")
        if resp and not Path(resp).is_absolute() and not (common.ROOT / resp).exists():
            resp = rel(common.WORKSPACE / resp) if (common.WORKSPACE / resp).exists() else resp
        out.append({"part": p.get("part"), "prompt_id": p.get("prompt_id"), "context": p.get("context"),
                    "prompt_file": rel(prompt) if prompt.exists() else None,
                    "prompt_sha": text_hash_of(prompt) if prompt.exists() else None,
                    "response_file": resp,
                    "response_sha": text_hash_of(_abs(resp)) if resp and _abs(resp).exists() else None})
    return out


def trace(conversation_id: str, evaluation_id: Optional[str] = None, criterion_id: Optional[str] = None,
          include_text: bool = False, until: Optional[str] = None) -> dict:
    """Where an answer came from: prompt (file + hash + prompt_id), raw response, every version and human action."""
    st = state.load(until)
    eid = evaluation_id or st.published.get(conversation_id)
    if not eid:
        evs = [e for e in st.evaluations.values() if e["conversation_id"] == conversation_id]
        eid = max(evs, key=lambda e: e["imported_at"])["evaluation_id"] if evs else None
    if not eid:
        raise CQAError(f"no evaluation of {conversation_id}", "not_found")
    ev = st.evaluations[eid]
    chain = [ev]
    while chain[-1].get("versions", {}).get("rescored_from") in st.evaluations:  # rescored → the LLM original
        chain.append(st.evaluations[chain[-1]["versions"]["rescored_from"]])
    data = events.get_blob(ev["blob"]) if ev.get("blob") else {}
    fin = st.final(eid)
    files = [f for e in chain for f in _prompt_files(e)]
    if include_text:
        for f in files:
            for key in ("prompt_file", "response_file"):
                if f.get(key):
                    f[key.replace("_file", "_text")] = _abs(f[key]).read_text(encoding="utf-8")
    doc = st.doc(ev["checklist"])
    ctx = [p.get("context") or {} for e in chain for p in (e.get("versions") or {}).get("prompts") or []]
    rules = sorted({u for c in ctx for u in (c.get("rules") or {}).values()})
    precs = sorted({d for c in ctx for d in c.get("precedents") or []})
    answers = {}
    for c in ck.active_criteria(doc):
        if criterion_id and c["id"] != criterion_id:
            continue
        item = (data.get("criteria") or {}).get(c["id"]) or {}
        dec = next((st.decisions_for(e["evaluation_id"]).get(c["id"]) for e in chain
                    if st.decisions_for(e["evaluation_id"]).get(c["id"])), None)  # decisions survive a rescore
        answers[c["id"]] = {
            "model_answer": ev["answers"].get(c["id"]), "final_answer": fin["final"]["answers"].get(c["id"]),
            "origin": ((ev.get("versions") or {}).get("answer_origin") or {}).get(c["id"], "llm"),
            "criterion_rev": ((ev.get("versions") or {}).get("checklist") or {}).get("criteria_revs", {}).get(c["id"]),
            "explanation": item.get("explanation"), "evidence": item.get("evidence"),
            "evidence_checks": item.get("evidence_checks"),
            "decision": dec and {k: dec.get(k) for k in ("decision_id", "reviewer_id", "action", "new_answer",
                                                         "type", "reason", "decided_at", "appeal_id", "simulated")}}
    human = [state.feed_row(e) for e in st.events if e.get("conversation_id") == conversation_id
             and e["type"] in ("appeal_filed", "appeal_assigned", "appeal_opened", "decision_made",
                               "precedent_marked", "precedent_unmarked", "evaluation_published",
                               "operator_assigned", "model_answer_revealed")]
    return {
        "conversation_id": conversation_id, "evaluation_id": eid, "lineage": [e["evaluation_id"] for e in chain],
        "source": f"{ev['run_id']}:{ev['agent']}", "imported_event": ev.get("id"),
        "versions": {
            "checklist": (ev.get("versions") or {}).get("checklist"),
            "template": (chain[-1].get("versions") or {}).get("template"),
            "params": (chain[-1].get("versions") or {}).get("params"),
            "features": (chain[-1].get("versions") or {}).get("features"),
            "models": (chain[-1].get("versions") or {}).get("models"),
            "code": (chain[-1].get("versions") or {}).get("code"),
            "rule_clarifications": rules, "precedents": precs},
        "prompts": files, "result_blob": ev.get("blob"),
        "total": {"original": fin["original"]["total"], "final": fin["final"]["total"],
                  "gross_error": fin["final"]["gross_error"]},
        "answers": answers, "human_actions": human,
        "verify": {"history_chain": "history.verify",
                   "blob": rel(common.BLOBS / ev["blob"][:2] / f"{ev['blob']}.json") if ev.get("blob") else None},
    }
