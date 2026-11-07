"""Simulated reviewer (sprint 1, item 2): ground-truth discrepancies become appeals and reviewer decisions.

For every conversation of a gold split, every criterion where the evaluation of `source` differs from the gold
label becomes an appeal filed by the conversation's operator, assigned round-robin to the given reviewers and
decided with the gold label; the reason is the gold comment. Type B when the comment refers to a rule of the
labeling guide (R1–R8) or an interpretation (patterns in config: simulation.type_b_patterns), otherwise A.
`noise` — share of deliberately wrong decisions (appeal rejected as unfounded, type D) to test the loop's
robustness. Everything carries `simulated: {batch, gold, noise}`; nothing runs without the button.
"""

from __future__ import annotations

import random
import re
from typing import Optional

from . import access, appeals, common, events, evaluations, groundtruth, metrics, sources, state
from .common import CQAError


def _type_for(comment: str) -> str:
    pats = common.setting("simulation.type_b_patterns", [r"\bR[1-8]\b", "трактов", "правил"])
    return "B" if any(re.search(p, comment or "", re.I) for p in pats) else "A"


def run(gold: str, source: str, actor: str, split: str = "tune", reviewers: Optional[list] = None,
        noise: float = 0.0, seed: int = 7, limit: int = 0, criteria: Optional[list] = None) -> dict:
    st = state.load()
    access.require(actor, "simulation.run", st)
    if not 0 <= float(noise) < 1:
        raise CQAError("noise must be in [0, 1)")
    reviewers = reviewers or [p for p, info in sorted(st.people.items()) if info.get("role") == "reviewer"][:3]
    bad = [r for r in reviewers if st.role_of(r) not in ("reviewer", "admin")]
    if not reviewers or bad:
        raise CQAError(f"reviewers {bad or reviewers} are not registered reviewers — register people first "
                       "(people.seed_demo + people.confirm_suggestions or people.register)", "bad_request")
    g = groundtruth.gold(gold, split)
    imp = evaluations.import_source(source, actor)  # idempotent: already imported results are skipped
    src = sources.resolve(source)
    run_id, agent = src["run_id"], src["agent"]
    st = state.load()
    batch = f"sim-{sum(1 for e in events.read(types=['simulation_finished'])) + 1:04d}"
    tag = {"batch": batch, "gold": g["label"], "split": split, "noise": float(noise)}
    rng = random.Random(f"{seed}:{gold}:{source}:{split}")
    events.append("simulation_started", actor=actor, batch=batch, gold=g["label"], source=source, split=split,
                  reviewers=reviewers, noise=float(noise), seed=seed)

    stats = {"conversations": 0, "criteria_compared": 0, "discrepancies": 0, "appeals": 0, "changed": 0,
             "noise_rejected": 0, "skipped_open": 0, "skipped_no_operator": 0, "missing_evaluation": 0,
             "by_type": {"A": 0, "B": 0, "D": 0}, "by_criterion": {}}
    items = []
    n = 0
    for conv_id, labels in sorted(g["items"].items()):
        eid = st.published.get(conv_id)
        if not eid or (st.evaluations[eid]["run_id"], st.evaluations[eid]["agent"]) != (run_id, agent):
            eid = st.latest_by_key.get((run_id, agent, conv_id))
        if not eid:
            stats["missing_evaluation"] += 1
            continue
        stats["conversations"] += 1
        operator = st.operator_of.get(conv_id)
        current = st.final(eid)["final"]["answers"]
        for cid, lab in sorted(labels.items()):
            if criteria and cid not in criteria:
                continue
            if lab.get("answer") is None:
                continue
            stats["criteria_compared"] += 1
            if metrics.key(current.get(cid)) == metrics.key(lab["answer"]):
                continue
            stats["discrepancies"] += 1
            stats["by_criterion"][cid] = stats["by_criterion"].get(cid, 0) + 1
            if limit and n >= limit:
                continue
            if not operator:
                stats["skipped_no_operator"] += 1
                continue
            reviewer = reviewers[n % len(reviewers)]
            comment = (f"Не согласен с ответом «{current.get(cid)}» по {cid}: по моему мнению — «{lab['answer']}». "
                       f"{lab.get('comment') or ''}").strip()
            try:
                ap = appeals.file(eid, cid, comment, operator, turn_idx=(lab.get("evidence_idx") or [None])[0],
                                  simulated=tag)["appeal_id"]
            except CQAError as e:
                if e.code == "conflict":
                    stats["skipped_open"] += 1
                    continue
                raise
            appeals.assign(ap, reviewer, actor, simulated=tag)
            wrong = rng.random() < float(noise)
            if wrong:
                d = appeals.decide(reviewer, "confirm", "Апелляция не обоснована: ответ модели соответствует "
                                   "чек-листу.", "D", appeal_id=ap, simulated={**tag, "wrong": True})
                stats["noise_rejected"] += 1
                stats["by_type"]["D"] += 1
            else:
                typ = _type_for(lab.get("comment") or "")
                reason = lab.get("comment") or f"Метка эталона {g['label']}."
                d = appeals.decide(reviewer, "change", reason, typ, appeal_id=ap, new_answer=lab["answer"],
                                   simulated=tag)
                stats["changed"] += 1
                stats["by_type"][typ] += 1
            stats["appeals"] += 1
            n += 1
            items.append({"conversation_id": conv_id, "criterion_id": cid, "appeal_id": ap,
                          "decision_id": d["decision_id"], "model": current.get(cid), "gold": lab["answer"],
                          "reviewer": reviewer, "wrong": wrong})
    events.append("simulation_finished", actor=actor, batch=batch, source=source, gold=g["label"], split=split,
                  stats=stats)
    return {"batch": batch, "source": source, "gold": g["label"], "split": split, "imported": imp["imported"],
            "stats": stats, "items": items}
