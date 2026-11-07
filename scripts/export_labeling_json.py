"""Export expert CSV labels to platform JSON and organizers' JSON 1.0 structure.

This exports ground truth, not a pipeline run or an official submission.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cqa.scoring import score

CODES = dict(zip((f"c{i:02}" for i in range(1, 9)), (
    "contact", "discovery", "identification", "consultation",
    "empathy", "dialogue", "closure", "critical",
)))


def export(csv_path: Path, output_dir: Path, team_name: str, model_name: str):
    checklist = json.loads((ROOT / "data/checklists/default/v1.json").read_text(encoding="utf-8"))
    criteria = {c["id"]: c for c in checklist["criteria"]}
    sources = {p.stem: json.loads(p.read_text(encoding="utf-8"))
               for p in (ROOT / "data/dev/conversations").glob("*.json")}
    groups = {}
    with csv_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            groups.setdefault(row["conversation_id"], []).append(row)
    if set(groups) != set(sources):
        raise ValueError("CSV conversation IDs must exactly match the DEV source set")
    results, items = [], {}
    for cid, rows in groups.items():
        counts = Counter(row["criterion_id"] for row in rows)
        if set(counts) != set(CODES) or any(n != 1 for n in counts.values()):
            raise ValueError(f"{cid}: expected each of the eight criteria exactly once")
        source_indices = {t["idx"] for t in sources[cid]["transcript"]}
        labels, exported, answers = {}, [], {}
        for row in sorted(rows, key=lambda r: r["criterion_id"]):
            key, raw = row["criterion_id"], row["answer"]
            spec = criteria[key]
            allowed = ({"yes", "no"} if spec["type"] in {"binary", "flag"}
                       else {str(s["value"]) for s in spec["scale"]})
            if spec.get("na_allowed"):
                allowed.add("na")
            if raw not in allowed:
                raise ValueError(f"{cid}/{key}: invalid answer {raw}")
            answer = int(raw) if raw.isdigit() else raw
            value = {"yes": True, "no": False, "na": None}.get(raw, answer)
            ev = [int(x) for x in row["evidence_idx"].split()]
            if not ev or not set(ev) <= source_indices or not row["comment"].strip():
                raise ValueError(f"{cid}/{key}: missing rationale or invalid source evidence")
            answers[key] = answer
            labels[key] = {"answer": answer, "evidence_idx": ev,
                           "comment": row["comment"], "source": "human"}
            exported.append({"criterion_id": CODES[key], "value": value,
                             "rationale": row["comment"], "evidence_turns": ev})
        calculated = score(checklist, answers)
        # Independently check weighted normalization and flag polarity.
        numerator, denominator = Decimal(0), Decimal(0)
        for key, answer in answers.items():
            spec = criteria[key]
            if spec["type"] == "flag" or answer == "na":
                continue
            weight = Decimal(str(spec["weight"]))
            denominator += weight
            fraction = (Decimal(int(answer == "yes")) if spec["type"] == "binary"
                        else Decimal(answer) / max(Decimal(str(s["value"])) for s in spec["scale"]))
            numerator += weight * fraction
        expected = float((100 * numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if expected != calculated["total"] or calculated["gross_error"] != (answers["c08"] == "yes"):
            raise ValueError(f"{cid}: scoring mismatch")
        results.append({"conversation_id": cid, "status": "ok", "criteria": exported,
                        "score": {"final_score": calculated["total"],
                                  "critical_flag": calculated["gross_error"]}})
        items[cid] = {"labels": labels, "annotator": rows[0]["annotator"]}
    output_dir.mkdir(parents=True, exist_ok=True)
    docs = {
        "all.json": {"checklist": "default@v1", "items": items},
        "results.json": {"schema_version": "1.0", "team_name": team_name,
                         "model": {"mode": "external_api", "name": model_name}, "results": results},
    }
    for name, doc in docs.items():
        target = output_dir / name
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if json.loads(target.read_text(encoding="utf-8")) != doc:
            raise ValueError(f"{target}: round-trip mismatch")
    print(f"Validated {len(results)} conversations, {sum(len(r['criteria']) for r in results)} labels; scores and source evidence checked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "docs/team/labeling/labels/all.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/team/labeling/labels")
    parser.add_argument("--team-name", default="altay+chat")
    parser.add_argument("--model-name", default="GPT-6 (Codex); expert review: altay")
    args = parser.parse_args()
    export(args.csv, args.out, args.team_name, args.model_name)
