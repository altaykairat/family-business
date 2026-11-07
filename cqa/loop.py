"""S10. The closed loop under human control (decisions 2, 7, 11; HX.*, CL.*, RG.*, CH.5).

  digest      algorithmic extract of the history (S10.2) — a checkable artifact, what the orchestrator will see
  proposals   stored suggestions (from the orchestrator or a human) with ops on a base checklist version;
              applied only by an admin («Принять» → new *draft* version; «Отклонить» → stays in the history)
  preview     consequences of a change before saving: diff, affected appeals/precedents/rules, new effective
              weights, how many LLM calls a re-evaluation would cost per decomposition variant (CH.5, M.14)
  regression  candidate vs baseline on ground truth + all decided appeals; passes only if the macro accuracy
              did not drop more than x p.p. and no precedent is broken (RG.1–RG.2)
  activation  only a human activates / rolls back a version; by default only after a passed regression
  simulate    «what if» for weights: recompute every total without any LLM call (decision 1, CL.E)
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from . import access, appeals, checklist as ck
from . import common, events, groundtruth, metrics, scoring, sources, state, stats
from .common import CQAError, load_json, save_json


# --- patterns with confidence (sprint 1, item 3) ---------------------------------------------------

def _situation(st: state.State, d: dict, cache: dict) -> dict:
    """S2 features of the decision's conversation: ending, channel, language, client dissatisfaction, waits."""
    from . import conversation as cv
    from . import db

    key = d["evaluation_id"]
    if key in cache:
        return cache[key]
    ev = st.evaluations.get(key) or {}
    feats = {}
    try:
        conv = cv.load(ev.get("dataset") or "dev", d["conversation_id"])
    except CQAError:
        conv = None
    blob = {}
    if ev.get("blob"):
        try:
            blob = events.get_blob(ev["blob"]) or {}
        except Exception:  # noqa: BLE001 — a missing blob only hides the model-derived feature
            blob = {}
    dis = ((blob.get("dialog_facts") or {}).get("client_dissatisfaction") or {}).get("present")
    if conv:
        facts = cv.facts(conv, int(common.load_config().get("gap_threshold_s", 30)))
        feats = {"ending": db.ending_of(conv), "channel": conv.get("channel"), "language": conv.get("language"),
                 "waits": "yes" if facts["operator_reply_gaps_s"] else "no"}
    feats["dissatisfaction"] = "unknown" if dis is None else ("yes" if dis else "no")
    cache[key] = feats
    return feats


def patterns(decisions: list, st: Optional[state.State] = None) -> dict:
    """Group reviewer decisions by «criterion × was → became × situation feature» and measure how sure the
    pattern is: cases, distinct reviewers, contradicting decisions (same criterion, same model answer, same
    situation, different reviewer answer — incl. confirmations) and the Wilson lower bound of the agreeing share.
    Groups above the thresholds (config loop.patterns) are «ready» for a proposal; the rest «accumulate»."""
    st = st or state.load()
    cfg = common.setting("loop.patterns", {}) or {}
    feats = cfg.get("features", ["ending", "channel", "language", "dissatisfaction", "waits"])
    min_cases = int(cfg.get("min_cases", 3))
    min_rev = int(cfg.get("min_reviewers", 2))
    max_contra = float(cfg.get("max_contradiction_share", 0.2))
    min_low = float(cfg.get("min_wilson_lower", 0.4))
    cache: dict = {}
    rows = []
    for d in decisions:
        if d.get("model_answer") is None:
            continue
        rows.append({"d": d, "crit": d["criterion_id"], "was": metrics.key(d.get("model_answer")),
                     "became": metrics.key(d.get("new_answer")), "sit": _situation(st, d, cache)})
    groups = {}
    for r in rows:
        if r["was"] == r["became"]:
            continue  # confirmations are not patterns of change — they count as contradictions below
        keys = [()] + [((f, r["sit"].get(f)),) for f in feats if r["sit"].get(f) is not None]
        for fk in keys:
            groups.setdefault((r["crit"], r["was"], r["became"], fk), []).append(r)
    out = []
    for (crit, was, became, fk), members in groups.items():
        cond = dict(fk)
        same = [r for r in rows if r["crit"] == crit and r["was"] == was
                and all(r["sit"].get(f) == v for f, v in cond.items())]
        contra = [r for r in same if r["became"] != became]
        n, c = len(members), len(contra)
        reviewers = sorted({r["d"]["reviewer_id"] for r in members})
        share = n / (n + c) if n + c else None
        low = (stats.wilson(n, n + c) or (None,))[0]
        reasons = []
        if n < min_cases:
            reasons.append(f"случаев {n} < {min_cases}")
        if len(reviewers) < min_rev:
            reasons.append(f"проверяющих {len(reviewers)} < {min_rev}")
        if c / (n + c) > max_contra:
            reasons.append(f"противоречий {c}/{n + c} > {max_contra:.0%}")
        if low is not None and low < min_low:
            reasons.append(f"нижняя граница {low} < {min_low}")
        out.append({
            "signature": {"criterion_id": crit, "was": was, "became": became, "situation": cond},
            "label": f"{crit}: {was} → {became}" + "".join(f" · {f}={v}" for f, v in cond.items()),
            "cases": n, "reviewers": reviewers, "contradictions": c, "agree_share": round(share, 4),
            "wilson_lower": low, "types": dict(Counter(r["d"]["type"] for r in members)),
            "simulated": sum(1 for r in members if r["d"].get("simulated")),
            "decision_ids": [r["d"]["decision_id"] for r in members],
            "contradicting_decision_ids": [r["d"]["decision_id"] for r in contra],
            "reasons_sample": [r["d"]["reason"] for r in members[:3]],
            "ready": not reasons, "not_ready_because": reasons})
    # a situation-specific group that holds exactly the same decisions as its base group adds nothing
    base = {(g["signature"]["criterion_id"], g["signature"]["was"], g["signature"]["became"]):
            (set(g["decision_ids"]), g["contradictions"]) for g in out if not g["signature"]["situation"]}
    out = [g for g in out if not g["signature"]["situation"] or
           base.get((g["signature"]["criterion_id"], g["signature"]["was"], g["signature"]["became"]))
           != (set(g["decision_ids"]), g["contradictions"])]
    out.sort(key=lambda g: (not g["ready"], -(g["wilson_lower"] or 0), -g["cases"], g["label"]))
    return {"thresholds": {"min_cases": min_cases, "min_reviewers": min_rev,
                           "max_contradiction_share": max_contra, "min_wilson_lower": min_low, "features": feats},
            "ready": [g for g in out if g["ready"]], "accumulating": [g for g in out if not g["ready"]]}


def gold_matches(d: dict, gold: str) -> bool:
    """Real decisions always match; a simulated one only if it used `gold` (set id or set@vN)."""
    sim = d.get("simulated")
    if not sim:
        return True
    used = sim.get("gold") or ""
    return used == gold or used.split("@")[0] == gold


# --- digest (S10.2) -------------------------------------------------------------------------

def build_digest(actor: str = "system", window: Optional[str] = None, gold: Optional[str] = None) -> dict:
    """`gold` — only decisions of simulated reviewers that used this gold set (R-flip: dev-gold-strict) plus no
    other simulated batches; real human decisions are always kept."""
    st = state.load()
    window = window or common.setting("loop.digest_window", "since_active_version")
    cid = "default"
    ref = ck.make_ref(*ck.resolve(cid))
    since = None
    if window == "since_active_version":
        acts = [e for e in st.checklist_events if e["type"] == "version_activated" and e.get("ref") == ref]
        since = acts[-1]["ts"] if acts else None
    decisions = [d for d in st.decisions.values() if since is None or d["decided_at"] >= since]
    if gold:
        decisions = [d for d in decisions if gold_matches(d, gold)]
    by_crit: dict = {}
    for d in decisions:
        b = by_crit.setdefault(d["criterion_id"], {"decisions": 0, "changed": 0, "types": Counter(), "cases": []})
        b["decisions"] += 1
        b["changed"] += d["action"] == "change"
        b["types"][d["type"]] += 1
        b["cases"].append({"decision_id": d["decision_id"], "conversation_id": d["conversation_id"],
                           "model": d.get("model_answer"), "reviewer": d["new_answer"], "type": d["type"],
                           "reason": d["reason"], "precedent": d["decision_id"] in st.precedent,
                           "judged_on": d.get("judged_on")})
    evals = [e for e in st.evaluations.values() if e["current"]]
    appeals_by_crit = Counter(a["criterion_id"] for a in st.appeals.values())
    evaluated_by_crit = Counter(cid for e in evals for cid in (e.get("answers") or {}))
    min_rule = int(common.setting("loop.min_cases_for_rule", 3))
    min_weight = int(common.setting("loop.min_cases_for_weight", 5))
    criteria = {}
    for crit, b in sorted(by_crit.items()):
        criteria[crit] = {
            "decisions": b["decisions"], "changed": b["changed"], "types": dict(b["types"]),
            "appeal_rate": stats.proportion(appeals_by_crit[crit], evaluated_by_crit[crit]),
            "ready_for_rule": b["types"]["A"] + b["types"]["B"] >= min_rule,
            "ready_for_weight": b["types"]["C"] >= min_weight,
            "cases": b["cases"],
        }
    last_metrics = list(st.metric_reports.values())[-1:] or []
    pats = patterns(decisions, st)
    digest = {
        "digest_id": f"dg-{len(st.digests) + 1:04d}", "checklist": ref, "window": window, "since": since,
        "gold_filter": gold,
        "rules": {"min_cases_for_rule": min_rule, "min_cases_for_weight": min_weight,
                  "weights_only_from_type": "C"},
        "totals": {"evaluations": len(evals), "appeals": len(st.appeals), "decisions": len(decisions),
                   "types": dict(Counter(d["type"] for d in decisions))},
        "criteria": criteria,
        "patterns": pats,
        "gaps_type_B": [c for b in by_crit.values() for c in b["cases"] if c["type"] == "B"],
        "conflicts": appeals.conflicts(),
        "precedents": len(st.precedent),
        "latest_metrics": [m.get("summary") for m in last_metrics],
        "open_proposals": [p["proposal_id"] for p in st.proposals.values() if p["status"] == "open"],
    }
    path = common.HISTORY_DIR / "digests" / f"{digest['digest_id']}.json"
    save_json(path, digest)
    events.append("digest_built", actor=actor, digest_id=digest["digest_id"], path=common.rel(path),
                  checklist=ref, decisions=len(decisions), patterns_ready=len(pats["ready"]),
                  patterns_accumulating=len(pats["accumulating"]))
    return digest


def for_orchestrator(digest: dict) -> dict:
    """What the orchestrator sees: only patterns above the thresholds; accumulating ones are counted, not shown,
    so a proposal is never built from a single or contested case."""
    ready = {d for g in digest["patterns"]["ready"] for d in g["decision_ids"]}
    crit = {}
    for cid, b in digest["criteria"].items():
        crit[cid] = {**{k: v for k, v in b.items() if k != "cases"},
                     "cases": [c for c in b["cases"] if c["decision_id"] in ready or c["type"] == "C"]}
    return {**digest, "criteria": crit,
            "patterns": {"thresholds": digest["patterns"]["thresholds"], "ready": digest["patterns"]["ready"],
                         "accumulating_count": len(digest["patterns"]["accumulating"])},
            "gaps_type_B": [c for c in digest["gaps_type_B"] if c["decision_id"] in ready]}


def analyze(actor: str, digest_id: Optional[str] = None, provider: Optional[str] = None, force: bool = False,
            gold: Optional[str] = None) -> dict:
    """«Проанализировать историю»: digest → orchestrator task, only when some pattern is above the thresholds
    (or type-C decisions are enough for a weight proposal). Otherwise nothing is sent: patterns keep accumulating."""
    from . import tasks

    digest = load_digest(digest_id) if digest_id else build_digest(actor, gold=gold)
    ready_c = [c for c, b in digest["criteria"].items() if b.get("ready_for_weight")]
    if not digest["patterns"]["ready"] and not ready_c and not force:
        return {"task": None, "digest_id": digest["digest_id"], "status": "accumulating",
                "message": "нет закономерностей выше порогов — предложение не готовится; см. patterns.accumulating",
                "patterns": digest["patterns"]}
    task = tasks.create("analyze_history", actor, {"digest_id": digest["digest_id"]}, provider)
    return {"task": task, "digest_id": digest["digest_id"], "status": "task_created",
            "ready_patterns": [g["label"] for g in digest["patterns"]["ready"]]}


def load_digest(digest_id: str) -> dict:
    path = common.HISTORY_DIR / "digests" / f"{digest_id}.json"
    if not path.exists():
        raise CQAError(f"digest {digest_id} not found", "not_found")
    return load_json(path)


# --- proposals (S10.4–S10.5) ------------------------------------------------------------------

def create_proposal(actor: str, kind: str, title: str, ops: list, rationale: str = "", base: Optional[str] = None,
                    criterion_id: Optional[str] = None, source: str = "human", task_id: Optional[str] = None,
                    evidence_decisions: Optional[list] = None, expected_effect: Optional[str] = None) -> dict:
    st = state.load()
    access.require(actor, "proposal.create", st)
    doc = ck.load(base or "default")
    error, warnings = None, []
    try:
        ck.apply_ops(doc, ops or [])
    except CQAError as e:
        error = e.message
    weight_ops = [o for o in ops or [] if o.get("op") == "set_weight"]
    if weight_ops:
        backing = [d for d in (evidence_decisions or []) if st.decisions.get(d, {}).get("type") == "C"]
        if not backing:
            warnings.append("weight change not backed by type C decisions (decision 4: only C feeds weight proposals)")
    proposal_id = f"p-{len(st.proposals) + 1:04d}"
    events.append("proposal_created", actor=actor, proposal_id=proposal_id, source=source, task_id=task_id,
                  kind=kind, title=title, rationale=rationale, ops=ops or [], base=ck.ref_of(doc),
                  criterion_id=criterion_id, evidence_decisions=evidence_decisions or [],
                  expected_effect=expected_effect, ops_valid=error is None, ops_error=error, warnings=warnings)
    return {"proposal_id": proposal_id, "ops_valid": error is None, "ops_error": error, "warnings": warnings}


def proposal(proposal_id: str) -> dict:
    p = state.load().proposals.get(proposal_id)
    if p is None:
        raise CQAError(f"proposal {proposal_id} not found", "not_found")
    return p


def accept(proposal_id: str, actor: str, reason: str = "", ops: Optional[list] = None) -> dict:
    """Admin accepts (optionally with edited ops) → a new checklist version (not active until activated)."""
    st = state.load()
    access.require(actor, "proposal.resolve", st)
    p = proposal(proposal_id)
    if p["status"] != "open":
        raise CQAError(f"proposal {proposal_id} is already {p['status']}", "conflict")
    base = ck.load(p["base"])
    new_doc = ck.apply_ops(base, ops if ops is not None else p["ops"])
    cid, ver = ck.parse_ref(p["base"])
    saved = ck.save_doc(new_doc, cid, actor, reason or f"{proposal_id}: {p['title']}", parent=ver,
                        source={"proposal_id": proposal_id, "origin": p.get("source")})
    events.append("proposal_accepted", actor=actor, proposal_id=proposal_id, resulting_version=saved["ref"],
                  reason=reason, edited_ops=ops if ops is not None else None, criterion_id=p.get("criterion_id"))
    return {"proposal_id": proposal_id, "new_version": saved["ref"], "status": "draft — run regression, then activate"}


def reject(proposal_id: str, actor: str, reason: str) -> dict:
    st = state.load()
    access.require(actor, "proposal.resolve", st)
    p = proposal(proposal_id)
    if p["status"] != "open":
        raise CQAError(f"proposal {proposal_id} is already {p['status']}", "conflict")
    if not (reason or "").strip():
        raise CQAError("a rejection needs a reason")
    events.append("proposal_rejected", actor=actor, proposal_id=proposal_id, reason=reason,
                  criterion_id=p.get("criterion_id"))
    return {"proposal_id": proposal_id, "status": "rejected"}


def change_checklist(actor: str, ops: list, reason: str, base: Optional[str] = None, save: bool = False) -> dict:
    """Direct human edit through the interface (HU.8). save=false → preview only."""
    st = state.load()
    access.require(actor, "checklist.change", st)
    doc = ck.load(base or "default")
    new_doc = ck.apply_ops(doc, ops)
    out = {"preview": preview(doc, new_doc)}
    if save:
        if not (reason or "").strip():
            raise CQAError("saving a version needs a reason (the commit message)")
        cid, ver = ck.parse_ref(ck.ref_of(doc))
        out["saved"] = ck.save_doc(new_doc, cid, actor, reason, parent=ver, source={"ops": ops})
    return out


# --- preview of consequences (CH.5, M.14) --------------------------------------------------------

def preview(old: dict, new: dict) -> dict:
    st = state.load()
    d = ck.diff_struct(old, new)
    touched = set(d["added"]) | set(d["removed"]) | set(d["archived"]) | set(d["changed"])
    semantic = set(d["added"]) | {c for c, f in d["changed"].items() if set(f) - ck.PLACEMENT_FIELDS}
    weight_only = {c for c, f in d["changed"].items() if not (set(f) - ck.PLACEMENT_FIELDS)}
    gone = set(d["removed"]) | set(d["archived"])
    labels_outdated = sorted(semantic - gone - set(d["added"]))
    cl = d.get("clarifications") or {}
    if cl.get("criteria"):  # a rule clarification changes the prompt of its criterion («*» = every criterion)
        semantic |= ({c["id"] for c in ck.active_criteria(new)} if "*" in cl["criteria"]
                     else set(cl["criteria"]) - {"*"})
        weight_only -= semantic
    semantic -= gone
    titles = {c["id"]: c.get("title", "") for c in old["criteria"]}
    rules_mentioning = [r for r in new.get("global_rules", [])
                        for c in gone if c in r or (titles.get(c) and titles[c].lower() in r.lower())]
    old_active = [c for c in ck.active_criteria(old) if c.get("weight", 0) > 0]
    new_active = [c for c in ck.active_criteria(new) if c.get("weight", 0) > 0]
    tw_new = sum(c["weight"] for c in new_active) or 1
    max_total = new.get("scoring", {}).get("max_total", 100)
    convs = {e["conversation_id"] for e in st.evaluations.values() if e["current"]}
    n_conv = len(convs)
    n_sections_touched = len({c.get("section") for c in new["criteria"] + old["criteria"] if c["id"] in semantic})
    return {
        "diff": d, "diff_text": ck.diff(old, new),
        "open_appeals_on_removed": [a["appeal_id"] for a in st.appeals.values()
                                    if a["criterion_id"] in gone and a["status"] in ("open", "assigned")],
        "precedents_to_archive": [did for did in st.precedent if st.decisions[did]["criterion_id"] in touched - weight_only],
        "global_rules_mentioning_removed": rules_mentioning,
        "nominal_weight_total": {"old": sum(c["weight"] for c in old_active), "new": sum(c["weight"] for c in new_active)},
        "effective_weights_without_na": {c["id"]: round(c["weight"] / tw_new * max_total, 2) for c in new_active},
        "ground_truth_labels_outdated": labels_outdated,
        "prompt_affected_criteria": sorted(semantic),
        "ground_truth_labels_missing": sorted(d["added"]),
        "reevaluation_llm_calls": {
            "weights_only": 0 if not semantic else None,
            "v1_monolith": n_conv if semantic else 0,
            "v2_sections": n_conv * n_sections_touched,
            "v3_per_criterion": n_conv * len(semantic),
            "conversations_evaluated": n_conv,
        },
        "rescore_without_llm": bool(weight_only and not semantic),
    }


def preview_refs(old_ref: str, new_ref: str) -> dict:
    return preview(ck.load(old_ref), ck.load(new_ref))


# --- regression (RG) and activation (MC.10–MC.11) --------------------------------------------------

def regression(candidate: str, actor: str, baseline: Optional[str] = None, gold: Optional[str] = None,
               split: str = "test", include_decisions: Optional[bool] = None) -> dict:
    st = state.load()
    access.require(actor, "metrics.run", st)
    include_decisions = common.setting("regression.include_decisions", True) if include_decisions is None else include_decisions
    max_drop = float(common.setting("regression.max_macro_drop_pp", 0.0)) / 100
    src, cand_results = sources.load_results(candidate)
    cand_ref = sources.checklist_ref(src, cand_results)
    checks, summary = [], {}
    if gold:
        g = groundtruth.gold(gold, split)
        doc = ck.load(g["checklist"])
        cr = metrics.evaluate(g, candidate, doc)
        summary["gold"] = g["label"]
        summary["candidate_macro"] = cr["macro_accuracy"]
        if baseline:
            br = metrics.evaluate(g, baseline, doc)
            summary["baseline_macro"] = br["macro_accuracy"]
            ok = cr["macro_accuracy"] is not None and br["macro_accuracy"] is not None and \
                cr["macro_accuracy"] >= br["macro_accuracy"] - max_drop
            checks.append({"check": f"macro accuracy not lower than baseline − {max_drop * 100:g} p.p.", "passed": ok,
                           "candidate": cr["macro_accuracy"], "baseline": br["macro_accuracy"]})
            fixed = {(e["conversation_id"], e["criterion_id"]) for e in br["errors"]} - \
                    {(e["conversation_id"], e["criterion_id"]) for e in cr["errors"]}
            broke = {(e["conversation_id"], e["criterion_id"]) for e in cr["errors"]} - \
                    {(e["conversation_id"], e["criterion_id"]) for e in br["errors"]}
            summary["fixed"] = sorted(map(list, fixed))
            summary["newly_broken"] = sorted(map(list, broke))
    broken_precedents = []
    if include_decisions:
        truth = st.human_truth()
        agree = total = 0
        for (conv, crit), d in truth.items():
            r = cand_results.get(conv)
            if not r or crit not in (r.get("answers") or {}):
                continue
            total += 1
            ok = metrics.key(r["answers"][crit]) == metrics.key(d["new_answer"])
            agree += ok
            if not ok and d["decision_id"] in st.precedent:
                broken_precedents.append({"decision_id": d["decision_id"], "conversation_id": conv,
                                          "criterion_id": crit, "expected": d["new_answer"],
                                          "got": r["answers"][crit]})
        summary["decisions_agreement"] = stats.proportion(agree, total)
        if common.setting("regression.require_no_broken_precedents", True):
            checks.append({"check": "no precedent broken", "passed": not broken_precedents,
                           "broken": broken_precedents})
    if not checks:
        raise CQAError("nothing to check: give a gold set with a baseline, or have decided appeals")
    passed = all(c["passed"] for c in checks)
    regression_id = f"rg-{len(st.regressions) + 1:04d}"
    report = {"regression_id": regression_id, "candidate": candidate, "baseline": baseline, "checklist_ref": cand_ref,
              "split": split, "passed": passed, "checks": checks, "summary": summary}
    path = common.REPORTS / f"regression-{regression_id}.json"
    save_json(path, report)
    events.append("regression_run", actor=actor, regression_id=regression_id, candidate=candidate, baseline=baseline,
                  checklist_ref=cand_ref, gold=gold, split=split, passed=passed,
                  candidate_macro=summary.get("candidate_macro"), baseline_macro=summary.get("baseline_macro"),
                  broken_precedents=len(broken_precedents), path=common.rel(path))
    return report


def activate(ref: str, actor: str, reason: str, profile: str = "default", force: bool = False) -> dict:
    st = state.load()
    access.require(actor, "version.activate", st)
    cid, ver = ck.resolve(ref)
    full = ck.make_ref(cid, ver)
    if ck.status_of(cid, ver) == "archived":
        raise CQAError(f"{full} is archived — restore it first", "conflict")
    current = ck.active_version(cid, profile)
    rollback = current is not None and ver < current
    passed = [r for r in st.regressions.values() if r.get("checklist_ref") == full and r.get("passed")]
    if common.setting("activation.require_passed_regression", True) and not passed and not rollback and ver != 1:
        if not force:
            raise CQAError(f"{full} has no passed regression run — run regression first, or force with a reason",
                           "conflict")
        if not (reason or "").strip():
            raise CQAError("forcing activation needs a reason")
    events.append("version_activated", actor=actor, kind="checklist", ref=full, checklist_id=cid, version=ver,
                  profile=profile, previous=current, rollback=rollback, forced=bool(force and not passed),
                  regression_id=passed[-1]["regression_id"] if passed else None, reason=reason)
    return {"active": full, "profile": profile, "previous": current and ck.make_ref(cid, current),
            "rollback": rollback, "note": "existing evaluations keep their version; re-evaluate with the button"}


def archive(ref: str, actor: str, reason: str, restore: bool = False) -> dict:
    st = state.load()
    access.require(actor, "checklist.change", st)
    cid, ver = ck.resolve(ref)
    full = ck.make_ref(cid, ver)
    if not restore and ck.active_version(cid) == ver:
        raise CQAError(f"{full} is active — activate another version first", "conflict")
    events.append("version_restored" if restore else "version_archived", actor=actor, kind="checklist", ref=full,
                  checklist_id=cid, version=ver, reason=reason)
    return {"ref": full, "status": "draft" if restore else "archived"}


# --- what-if for weights (CL.E, decision 1) ----------------------------------------------------------

def simulate(weights: Optional[dict] = None, checklist_ref: Optional[str] = None, source: Optional[str] = None,
             use_final: bool = True) -> dict:
    """Recompute totals of evaluations with other weights or another checklist version — no LLM calls."""
    st = state.load()
    cats = common.setting("analytics.categories", [])

    def category(total):
        if total is None:
            return None
        for c in sorted(cats, key=lambda x: -x["min"]):
            if total >= c["min"]:
                return c["name"]
        return None

    rows = []
    for eid, e in st.evaluations.items():
        if not e["current"]:
            continue
        if source:
            src = sources.resolve(source)
            if (e["run_id"], e["agent"]) != (src["run_id"], src["agent"]):
                continue
        elif not e["published"]:
            continue
        base_doc = st.doc(e["checklist"])
        answers = st.final(eid)["final"]["answers"] if use_final else e["answers"]
        alt = ck.load(checklist_ref) if checklist_ref else base_doc
        if weights:
            alt = ck.apply_ops(alt, [{"op": "set_weight", "criterion_id": k, "value": v} for k, v in weights.items()])
        old_t = scoring.score(base_doc, answers)["total"]
        new_t = scoring.score(alt, answers)["total"]
        rows.append({"evaluation_id": eid, "conversation_id": e["conversation_id"], "old_total": old_t,
                     "new_total": new_t, "delta": None if old_t is None or new_t is None else round(new_t - old_t, 2),
                     "old_category": category(old_t), "new_category": category(new_t)})
    if not rows:
        raise CQAError("no evaluations to simulate: publish evaluations or pass source=run:agent", "not_found")
    deltas = [r["delta"] for r in rows if r["delta"] is not None]
    moved = [r for r in rows if r["old_category"] != r["new_category"]]
    return {"weights": weights, "checklist": checklist_ref, "evaluations": len(rows),
            "mean_delta": stats.mean(deltas), "max_abs_delta": max((abs(x) for x in deltas), default=None),
            "category_changes": len(moved), "llm_calls": 0, "rows": rows}
