"""S7. Deterministic scoring by the checklist rules. No LLM involved.

Rules (docs/checklist.md, «Порядок расчёта»):
  1. N/A only where allowed (checked earlier, in validation).
  2. Yes = full weight, No = 0; scale step → weight × step / top step.
  3. Criteria with N/A are excluded; weights of applicable criteria are scaled so the max stays 100.
  4. Total is rounded half-up to `rounding` digits; no applicable weighted criteria → total is None.
  5. Flag criteria (gross errors) carry no points and are reported separately.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .checklist import active_criteria


def _q(value: Decimal, digits: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def criterion_points(c: dict, answer) -> Optional[Decimal]:
    """Raw points before N/A redistribution; None when the criterion is not scored."""
    weight = Decimal(str(c.get("weight", 0)))
    if c["type"] == "flag" or answer in (None, "na"):
        return None
    if c["type"] == "binary":
        return weight if answer == "yes" else Decimal(0)
    if c["type"] == "scale":
        top = max(Decimal(str(s["value"])) for s in c["scale"])
        return weight * Decimal(str(answer)) / top if top else Decimal(0)
    raise ValueError(f"unknown type {c['type']}")


def score(doc: dict, answers: dict) -> dict:
    """answers: {criterion_id: 'yes'|'no'|'na'|number}. Returns rows, sections, total, gross flags."""
    digits = int(doc.get("scoring", {}).get("rounding", 2))
    max_total = Decimal(str(doc.get("scoring", {}).get("max_total", 100)))
    crit = active_criteria(doc)

    raw = {}
    applicable_weight = Decimal(0)
    for c in crit:
        p = criterion_points(c, answers.get(c["id"]))
        if p is not None and Decimal(str(c.get("weight", 0))) > 0:
            raw[c["id"]] = p
            applicable_weight += Decimal(str(c["weight"]))

    def scaled(x: Decimal) -> Decimal:
        return x / applicable_weight * max_total

    rows, flags, missing = [], [], []
    for c in crit:
        a = answers.get(c["id"])
        row = {"criterion_id": c["id"], "title": c["title"], "section": c.get("section"), "type": c["type"],
               "answer": a, "weight": c.get("weight", 0), "applicable": c["id"] in raw,
               "points": None, "effective_weight": None, "effective_points": None}
        if a is None:
            missing.append(c["id"])
        if c["type"] == "flag":
            row["flag_raised"] = a == "yes"
            if a == "yes":
                flags.append(c["id"])
        if c["id"] in raw:
            w = Decimal(str(c["weight"]))
            row["points"] = _q(raw[c["id"]], digits)
            row["effective_weight"] = _q(scaled(w), digits)
            row["effective_points"] = _q(scaled(raw[c["id"]]), digits)
        rows.append(row)

    total = _q(scaled(sum(raw.values(), Decimal(0))), digits) if applicable_weight > 0 else None

    sections = []
    for s in doc.get("sections", []):
        ids = [c["id"] for c in crit if c.get("section") == s["id"] and c["id"] in raw]
        if not ids:
            continue
        w = sum((Decimal(str(next(c for c in crit if c["id"] == i)["weight"])) for i in ids), Decimal(0))
        p = sum((raw[i] for i in ids), Decimal(0))
        sections.append({"section_id": s["id"], "title": s["title"],
                         "score_pct": _q(p / w * 100, digits) if w else None,
                         "contribution": _q(scaled(p), digits), "max_contribution": _q(scaled(w), digits)})

    return {"total": total, "gross_error": bool(flags), "gross_error_criteria": flags,
            "complete": not missing, "missing_criteria": missing,
            "applicable_weight": float(applicable_weight), "rows": rows, "sections": sections}
