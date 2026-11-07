"""Compare a saved run with team labels. These labels are not organizer Ground Truth."""

import argparse
import json
from pathlib import Path

from engine import score, validate_answers
from store import Store

ROOT = Path(__file__).resolve().parents[1]


def compare(labels, store):
    exact = total = 0
    na_true_positive = na_predicted = na_actual = 0
    scale_exact = scale_total = 0
    evidence_hits = evidence_total = 0
    score_errors = []
    missing = []
    per_criterion = {}
    models = {}
    for item in labels:
        cid = item["conversation_id"]
        candidates = store.evaluations_for(cid)
        if not candidates:
            missing.append(cid)
            continue
        evaluation = next((store.evaluation(candidate["id"]) for candidate in candidates
                           if candidate["source"] not in {"mock-technical-demo", "manual"}), None)
        if evaluation is None:
            missing.append(cid + " (no live AI run; mock/manual excluded)")
            continue
        checklist = evaluation["checklist_snapshot"]
        conversation = store.conversation(cid)
        model_event = next((event["detail"] for event in reversed(evaluation["events"])
                            if event["kind"] == "ai_draft" and event["detail"].get("provider") != "mock"), None)
        if model_event is None:
            missing.append(cid + " (no live AI trace)")
            continue
        predicted = model_event["answers"]
        models[evaluation["source"]] = models.get(evaluation["source"], 0) + 1
        reference = item["answers"]
        if validate_answers(checklist, conversation, predicted) or validate_answers(checklist, conversation, reference):
            missing.append(cid + " (incomplete)")
            continue
        for criterion in checklist["criteria"]:
            key = criterion["id"]
            p, a = predicted[key], reference[key]
            p_answer = "na" if p["applicability"] == "na" else p["answer"]
            a_answer = "na" if a["applicability"] == "na" else a["answer"]
            matched = p_answer == a_answer
            exact += matched
            total += 1
            bucket = per_criterion.setdefault(key, {"exact": 0, "total": 0})
            bucket["exact"] += matched
            bucket["total"] += 1
            p_na, a_na = p["applicability"] == "na", a["applicability"] == "na"
            na_predicted += p_na
            na_actual += a_na
            na_true_positive += p_na and a_na
            if criterion["type"] == "scale":
                scale_total += 1
                scale_exact += matched
            evidence_total += 1
            evidence_hits += bool(set(p["evidence_idx"]) & set(a["evidence_idx"]))
        p_score = score(checklist, conversation, predicted)["total"]
        a_score = score(checklist, conversation, reference)["total"]
        if p_score is not None and a_score is not None:
            score_errors.append(abs(p_score - a_score))
    return {
        "label_source": "team_manual_labels_not_organizer_ground_truth",
        "prediction_source": "saved_live_ai_trace_before_manual_corrections",
        "models": models,
        "evaluated_conversations": len(labels) - len(missing), "missing_or_incomplete": missing,
        "exact_agreement": exact / total if total else None,
        "na_precision": na_true_positive / na_predicted if na_predicted else None,
        "na_recall": na_true_positive / na_actual if na_actual else None,
        "scale_exact_agreement": scale_exact / scale_total if scale_total else None,
        "score_mae": sum(score_errors) / len(score_errors) if score_errors else None,
        "evidence_index_overlap": evidence_hits / evidence_total if evidence_total else None,
        "per_criterion": per_criterion,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", type=Path, help="JSON list of {conversation_id, answers}")
    parser.add_argument("--db", type=Path, default=ROOT / "prototype" / "state.db")
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    store = Store(args.db, ROOT / "data" / "checklists" / "default.json")
    print(json.dumps(compare(labels, store), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
