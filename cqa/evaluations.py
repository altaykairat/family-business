"""Evaluations as facts of the history: import of run results, S6 aggregation into an ensemble, publication,
the conversation card (transcript + original + decisions + final).

An imported evaluation is immutable: its full result file is stored as a blob, the event keeps the answers,
total, checklist reference and the full version bundle of the run (VC.7). Re-importing a changed result
file creates a new evaluation that supersedes the old one; the old one stays in the history.
"""

from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Optional

from . import checklist as ck
from . import common, events, scoring, sources, state
from . import conversation as cv
from .common import CQAError, save_json, stable_hash


def import_source(spec: str, actor: str = "system", ids: Optional[list] = None) -> dict:
    """Import every result file of a source as an evaluation (skips already imported, unchanged ones)."""
    src, results = sources.load_results(spec, ids)
    st = state.load()
    manifest = src.get("manifest") or {}
    run_id = src["run_id"]
    imported, skipped = [], 0
    for conv_id, data in results.items():
        ref = data.get("checklist") or manifest.get("checklist")
        if not ref:
            raise CQAError(f"{conv_id}: result has no checklist reference")
        h = stable_hash(data, 8)
        evaluation_id = f"{run_id}:{src['agent']}:{conv_id}:{h}"
        if evaluation_id in st.evaluations:
            skipped += 1
            continue
        doc = st.doc(ref)
        answers = data.get("answers") or {}
        sc = scoring.score(doc, answers)  # recomputed by the platform's formula, never trusted from the file
        key = (run_id, src["agent"], conv_id)
        blob = events.put_blob(data)
        versions = manifest.get("versions") or {"checklist": {"ref": ref, "hash": manifest.get("checklist_hash"),
                                                             "criteria_revs": ck.criterion_revs(doc)},
                                                "template": manifest.get("template"),
                                                "params": {"hash": manifest.get("params_hash")}}
        parts = ((manifest.get("conversations") or {}).get(conv_id) or {}).get("parts") or []
        if parts:  # what exactly went into the prompt of this conversation: rules, precedents, prompt ids
            versions = {**versions, "prompts": [{k: p.get(k) for k in ("part", "file", "prompt_id", "context")}
                                                for p in parts]}
        events.append("evaluation_imported", actor=actor, evaluation_id=evaluation_id, run_id=run_id,
                      agent=src["agent"], conversation_id=conv_id, dataset=sources.dataset_of(src),
                      checklist=ref, variant=data.get("variant") or manifest.get("variant"),
                      status=data.get("status"), answers=answers, total=sc["total"], gross_error=sc["gross_error"],
                      complete=sc["complete"], blob=blob, source=spec, supersedes=st.latest_by_key.get(key),
                      versions=versions, usage=_usage_summary(data))
        imported.append(evaluation_id)
    return {"source": spec, "run_id": run_id, "agent": src["agent"], "imported": len(imported),
            "already_imported": skipped, "evaluation_ids": imported}


def _usage_summary(data: dict) -> dict:
    rows = data.get("usage") or []
    return {"calls": len(rows), "tokens_in": sum(r.get("tokens_in") or 0 for r in rows),
            "tokens_out": sum(r.get("tokens_out") or 0 for r in rows),
            "exact": all(r.get("exact") for r in rows) if rows else False}


# --- S6: ensemble of agents ------------------------------------------------------------

def majority(values: list, c: dict):
    """Majority across agents; for scales falls back to the median step when there is no majority."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, "нет ответов"
    top, n = Counter(map(str, vals)).most_common(1)[0]
    if n > len(vals) / 2:
        return next(v for v in vals if str(v) == top), f"{n}/{len(vals)}"
    nums = [v for v in vals if isinstance(v, (int, float))]
    if c["type"] == "scale" and len(nums) == len(vals):
        steps = sorted(s["value"] for s in c["scale"])
        m = median(nums)
        return min(steps, key=lambda s: (abs(s - m), s)), f"медиана из {len(vals)}"
    return None, f"нет большинства ({dict(Counter(map(str, vals)))})"


def aggregate(run_id: str, agents: Optional[list] = None, name: str = "majority", actor: str = "system") -> dict:
    """Write runs/<run>/validated/<name>/<id>.json: per criterion the majority answer, explanation and evidence
    from an agent that gave it (A.4), agreement (A.5) and a review flag when there is no majority (A.3)."""
    run_dir = common.RUNS / run_id
    manifest = common.load_json(run_dir / "manifest.json")
    agents = agents or manifest["agents"]
    doc = ck.load(manifest["checklist"])
    per_agent = {}
    for a in agents:
        _, res = sources.load_results(str(run_dir / "validated" / a)) if (run_dir / "validated" / a).exists() else (None, {})
        per_agent[a] = res
    conv_ids = sorted(set().union(*[set(r) for r in per_agent.values()])) if per_agent else []
    written = 0
    for conv_id in conv_ids:
        answered = [a for a in agents if conv_id in per_agent[a]]
        answers, criteria, agreement, issues, usage = {}, {}, {}, [], []
        for c in ck.active_criteria(doc):
            vals = {a: per_agent[a][conv_id].get("answers", {}).get(c["id"]) for a in answered}
            m, note = majority(list(vals.values()), c)
            agreement[c["id"]] = {"note": note, "answers": vals,
                                  "share": (sum(str(v) == str(m) for v in vals.values()) / len(vals)) if vals else 0}
            if m is None:
                issues.append({"level": "warn", "criterion_id": c["id"], "msg": f"нужна проверка человеком: {note}"})
                continue
            answers[c["id"]] = m
            donor = next((a for a in answered if str(vals[a]) == str(m)), None)
            item = dict((per_agent[donor][conv_id].get("criteria") or {}).get(c["id"], {})) if donor else {}
            item.update(answer_normalized=m, aggregated_from=donor, agreement=agreement[c["id"]]["share"])
            criteria[c["id"]] = item
        for a in answered:
            usage += per_agent[a][conv_id].get("usage") or []
        result = {"run_id": run_id, "agent": name, "conversation_id": conv_id, "checklist": manifest["checklist"],
                  "variant": manifest.get("variant"), "status": "warn" if issues else "ok",
                  "aggregation": {"method": "majority / median step", "agents": answered},
                  "agreement": agreement, "usage": usage, "score": scoring.score(doc, answers),
                  "answers": answers, "criteria": criteria, "issues": issues}
        save_json(run_dir / "validated" / name / f"{conv_id}.json", result)
        written += 1
    events.append("evaluations_aggregated", actor=actor, run_id=run_id, agents=agents, name=name,
                  conversations=written)
    return {"run_id": run_id, "name": name, "conversations": written, "source": f"{run_id}:{name}"}


# --- routing to a reviewer (sprint 1, item 4: insufficient_data is used here) -------------------

def review_signals(data: dict, min_confidence: float) -> dict:
    """{criterion_id: [reasons]} — why a human should look at this answer. Signals only, nothing is decided."""
    out = {}
    agreement = data.get("agreement") or {}
    for cid, item in (data.get("criteria") or {}).items():
        why = []
        if item.get("insufficient_data"):
            why.append("insufficient_data")
        if item.get("ambiguities"):
            why.append("ambiguities")
        conf = item.get("confidence")
        if isinstance(conf, (int, float)) and conf < min_confidence:
            why.append(f"confidence<{min_confidence}")
        if any(c.get("exists") is False or c.get("quote_ok") is False for c in item.get("evidence_checks") or []):
            why.append("evidence_check_failed")
        share = (agreement.get(cid) or {}).get("share")
        if share is not None and share < 1:
            why.append("models_disagree")
        if why:
            out[cid] = why
    for cid, a in agreement.items():  # no majority at all → the criterion has no item
        if cid not in (data.get("criteria") or {}):
            out[cid] = ["no_majority"]
    return out


def review_queue(source: Optional[str] = None, reasons: Optional[list] = None, until: Optional[str] = None) -> dict:
    """Which conversation × criterion answers to show a reviewer first: of a source (`run:agent`) or, by default,
    of the published evaluations. Ordered by the number of signals."""
    min_conf = float(common.setting("routing.min_confidence", 0.7))
    if source:
        _, results = sources.load_results(source)
        items = [(conv, data, None) for conv, data in results.items()]
    else:
        st = state.load(until)
        items = []
        for conv, eid in st.published.items():
            ev = st.evaluations[eid]
            items.append((conv, events.get_blob(ev["blob"]) if ev.get("blob") else {}, eid))
    queue = []
    for conv, data, eid in items:
        for cid, why in review_signals(data, min_conf).items():
            if reasons and not set(reasons) & {w.split("<")[0] for w in why}:
                continue
            queue.append({"conversation_id": conv, "criterion_id": cid, "evaluation_id": eid,
                          "answer": (data.get("answers") or {}).get(cid), "reasons": why})
    queue.sort(key=lambda q: (-len(q["reasons"]), q["conversation_id"], q["criterion_id"]))
    convs = {q["conversation_id"] for q in queue}
    return {"source": source or "published", "min_confidence": min_conf, "items": queue,
            "conversations": len(convs), "of": len(items),
            "by_reason": dict(Counter(w.split("<")[0] for q in queue for w in q["reasons"]))}


# --- publication ----------------------------------------------------------------------------

def publish(evaluation_ids: list, actor: str) -> dict:
    st = state.load()
    done = []
    for eid in evaluation_ids:
        ev = st.evaluation(eid)
        if st.published.get(ev["conversation_id"]) == eid:
            continue
        events.append("evaluation_published", actor=actor, evaluation_id=eid, conversation_id=ev["conversation_id"],
                      replaces=st.published.get(ev["conversation_id"]))
        done.append(eid)
    return {"published": done}


def publish_source(spec: str, actor: str) -> dict:
    src = sources.resolve(spec)
    st = state.load()
    ids = [eid for key, eid in st.latest_by_key.items() if key[0] == src["run_id"] and key[1] == src["agent"]]
    if not ids:
        raise CQAError(f"nothing imported from {spec} yet — run eval.import first", "not_found")
    return publish(ids, actor)


# --- the card --------------------------------------------------------------------------------

def details(evaluation_id: str, st: Optional[state.State] = None) -> dict:
    st = st or state.load()
    ev = st.evaluation(evaluation_id)
    return events.get_blob(ev["blob"])


def card(conversation_id: str, evaluation_id: Optional[str] = None, viewer: Optional[str] = None,
         until: Optional[str] = None) -> dict:
    """Everything the result card needs: transcript with idx and offsets, every criterion with answer, points,
    weight, effective weight, explanation, evidence, checked range; decisions and final; appeals; history."""
    st = state.load(until)
    evs = [e for e in st.evaluations.values() if e["conversation_id"] == conversation_id]
    if evaluation_id is None:
        evaluation_id = st.published.get(conversation_id) or (
            max(evs, key=lambda e: e["imported_at"])["evaluation_id"] if evs else None)
    ev = st.evaluations.get(evaluation_id) if evaluation_id else None
    dataset = (ev or {}).get("dataset") or "dev"
    conv = cv.load(dataset, conversation_id)
    out = {
        "conversation": {"conversation_id": conversation_id, "dataset": dataset, "channel": conv["channel"],
                         "language": conv["language"], "audio_url": conv.get("audio_url"),
                         "operator_id": st.operator_of.get(conversation_id),
                         "operator_suggestion": st.suggested_operator.get(conversation_id),
                         "transcript": [{"idx": t["idx"], "timestamp": t["timestamp"], "offset_s": t["t"],
                                         "speaker": t["speaker"], "text": t["text"]} for t in conv["transcript"]]},
        "evaluations": [{"evaluation_id": e["evaluation_id"], "run_id": e["run_id"], "agent": e["agent"],
                         "checklist": e["checklist"], "total": e["total"], "current": e["current"],
                         "published": e["published"]} for e in evs],
        "evaluation": None,
    }
    if ev:
        doc = st.doc(ev["checklist"])
        data = events.get_blob(ev["blob"])
        fin = st.final(evaluation_id)
        decided = st.decisions_for(evaluation_id)
        blind = viewer and st.reviewer_settings.get(viewer, {}).get("blind") and not any(
            r["reviewer_id"] == viewer and r.get("evaluation_id") == evaluation_id for r in st.reveals)
        rows = {r["criterion_id"]: r for r in fin["final"]["score"]["rows"]}
        orig_rows = {r["criterion_id"]: r for r in scoring.score(doc, ev["answers"])["rows"]}
        crit = []
        for c in ck.active_criteria(doc):
            item = (data.get("criteria") or {}).get(c["id"], {})
            d = decided.get(c["id"])
            crit.append({
                "criterion_id": c["id"], "title": c["title"], "section": c.get("section"), "type": c["type"],
                "weight": c.get("weight"), "na_allowed": c.get("na_allowed", False),
                "original": None if blind else {
                    "answer": ev["answers"].get(c["id"]), "points": orig_rows[c["id"]]["points"],
                    "effective_weight": orig_rows[c["id"]]["effective_weight"],
                    "effective_points": orig_rows[c["id"]]["effective_points"],
                    "explanation": item.get("explanation"), "evidence": item.get("evidence"),
                    "evidence_checks": item.get("evidence_checks"), "checked_range": item.get("checked_range"),
                    "confidence": item.get("confidence"), "agreement": item.get("agreement"),
                    "reasoning": item.get("reasoning"), "insufficient_data": bool(item.get("insufficient_data")),
                    "ambiguities": item.get("ambiguities") or []},
                "decision": d and {k: d.get(k) for k in ("decision_id", "action", "new_answer", "reason", "type",
                                                         "reviewer_id", "decided_at", "appeal_id")},
                "final": {"answer": fin["final"]["answers"].get(c["id"]), "points": rows[c["id"]]["points"],
                          "effective_weight": rows[c["id"]]["effective_weight"],
                          "effective_points": rows[c["id"]]["effective_points"]},
                "appeals": [a["appeal_id"] for a in st.appeals.values()
                            if a["evaluation_id"] == evaluation_id and a["criterion_id"] == c["id"]],
            })
        out["evaluation"] = {
            "evaluation_id": evaluation_id, "run_id": ev["run_id"], "agent": ev["agent"],
            "checklist": ev["checklist"], "versions": ev.get("versions"), "status": ev.get("status"),
            "blind": bool(blind),
            "original_total": None if blind else fin["original"]["total"],
            "final_total": fin["final"]["total"], "gross_error": fin["final"]["gross_error"],
            "original_gross_error": fin["original"]["gross_error"],
            "sections": fin["final"]["score"]["sections"], "criteria": crit,
            "dialog_facts": None if blind else data.get("dialog_facts"),
            "issues": data.get("issues"),
        }
    out["history"] = [state.feed_row(e) for e in st.events if e.get("conversation_id") == conversation_id]
    return out
