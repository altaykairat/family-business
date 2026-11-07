"""Rubric validation and deterministic scoring. No model output is trusted here."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


class Invalid(ValueError):
    pass


def validate_checklist(checklist: dict) -> None:
    criteria = checklist.get("criteria")
    sections = checklist.get("sections", [])
    if not isinstance(criteria, list) or not criteria:
        raise Invalid("Checklist needs at least one criterion")
    section_ids = {section.get("id") for section in sections}
    if len(section_ids) != len(sections):
        raise Invalid("Section IDs must be unique")
    ids = set()
    for criterion in criteria:
        cid = criterion.get("id")
        if not isinstance(cid, str) or not cid or cid in ids:
            raise Invalid("Criterion IDs must be unique and nonempty")
        ids.add(cid)
        if criterion.get("section") not in section_ids:
            raise Invalid(f"{cid}: unknown section")
        if not isinstance(criterion.get("title"), str) or not criterion["title"].strip():
            raise Invalid(f"{cid}: title is required")
        kind = criterion.get("type")
        if kind not in {"binary", "scale", "flag"}:
            raise Invalid(f"{cid}: unsupported type")
        weight = criterion.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            raise Invalid(f"{cid}: invalid weight")
        if kind == "flag" and weight != 0:
            raise Invalid(f"{cid}: flag weight must be zero")
        if kind != "flag" and weight <= 0:
            raise Invalid(f"{cid}: scored criterion needs positive weight")
        if kind == "scale":
            scale = criterion.get("scale")
            if not isinstance(scale, list) or not scale:
                raise Invalid(f"{cid}: scale is required")
            values = [step.get("value") for step in scale]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in values):
                raise Invalid(f"{cid}: invalid scale step")
            if len(set(values)) != len(values) or max(values) <= 0:
                raise Invalid(f"{cid}: scale steps must be unique with positive top step")
        if criterion.get("na_allowed") and not str(criterion.get("na_condition") or "").strip():
            raise Invalid(f"{cid}: N/A condition is required")


def normalize_conversation(record: dict) -> dict:
    cid = record.get("conversation_id")
    transcript = record.get("transcript")
    if not isinstance(cid, str) or not cid.strip():
        raise Invalid("conversation_id is required")
    if not isinstance(transcript, list) or not transcript:
        raise Invalid("transcript must contain messages")
    normalized = []
    for index, message in enumerate(transcript, 1):
        if not isinstance(message, dict) or message.get("speaker") not in {"client", "operator", "bot", "system"}:
            raise Invalid(f"message {index}: invalid speaker")
        if not isinstance(message.get("text"), str):
            raise Invalid(f"message {index}: text is required")
        normalized.append({**message, "idx": index})
    return {**record, "transcript": normalized}


def blank_answers(checklist: dict) -> dict:
    return {c["id"]: {"applicability": None, "na_reason": "", "answer": None,
                      "evidence_idx": [], "explanation": ""}
            for c in checklist["criteria"]}


def answer_errors(criterion: dict, answer: dict, message_count: int) -> list[str]:
    cid = criterion["id"]
    errors = []
    applicability = answer.get("applicability")
    if not isinstance(applicability, str) or applicability not in {"applicable", "na"}:
        errors.append(f"{cid}: choose applicability")
    elif applicability == "na" and not criterion.get("na_allowed"):
        errors.append(f"{cid}: N/A is forbidden")
    value = answer.get("answer")
    if applicability == "na":
        if value is not None:
            errors.append(f"{cid}: N/A must not have a score")
        if not isinstance(answer.get("na_reason"), str) or not answer["na_reason"].strip():
            errors.append(f"{cid}: N/A reason is required")
    elif applicability == "applicable":
        if criterion["type"] in {"binary", "flag"} and (not isinstance(value, str) or value not in {"yes", "no"}):
            errors.append(f"{cid}: answer must be yes or no")
        if criterion["type"] == "scale":
            allowed = {step["value"] for step in criterion["scale"]}
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in allowed:
                errors.append(f"{cid}: answer must match a scale step")
    evidence = answer.get("evidence_idx")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{cid}: evidence message is required")
    elif any(type(idx) is not int or idx < 1 or idx > message_count for idx in evidence):
        errors.append(f"{cid}: evidence index is outside transcript")
    elif len(set(evidence)) != len(evidence):
        errors.append(f"{cid}: evidence indices must be unique")
    if not isinstance(answer.get("explanation"), str) or not answer["explanation"].strip():
        errors.append(f"{cid}: explanation is required")
    return errors


def validate_answers(checklist: dict, conversation: dict, answers: dict) -> list[str]:
    expected = {c["id"] for c in checklist["criteria"]}
    errors = [f"unknown criterion: {cid}" for cid in answers if cid not in expected]
    for criterion in checklist["criteria"]:
        answer = answers.get(criterion["id"])
        if not isinstance(answer, dict):
            errors.append(f"{criterion['id']}: missing answer")
        else:
            errors.extend(answer_errors(criterion, answer, len(conversation["transcript"])))
    return errors


def _round(value: Decimal, places: int = 2) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def score(checklist: dict, conversation: dict, answers: dict) -> dict:
    errors = validate_answers(checklist, conversation, answers)
    if errors:
        raise Invalid("; ".join(errors))
    rows = []
    exact_points = {}
    for criterion in checklist["criteria"]:
        answer = answers[criterion["id"]]
        weight = Decimal(str(criterion["weight"]))
        points = None
        if answer["applicability"] == "applicable":
            if criterion["type"] == "binary":
                points = weight if answer["answer"] == "yes" else Decimal(0)
            elif criterion["type"] == "scale":
                top = Decimal(str(max(step["value"] for step in criterion["scale"])))
                points = weight * Decimal(str(answer["answer"])) / top
        exact_points[criterion["id"]] = points
        rows.append({"criterion_id": criterion["id"], "section": criterion["section"],
                     "weight": float(weight), "raw_points": None if points is None else _round(points),
                     "applicability": answer["applicability"], "answer": answer["answer"]})

    def subtotal(subset: list[dict]) -> float | None:
        weighted = [row for row in subset if row["raw_points"] is not None and row["weight"] > 0]
        denominator = sum(Decimal(str(row["weight"])) for row in weighted)
        if denominator == 0:
            return None
        numerator = sum(exact_points[row["criterion_id"]] for row in weighted)
        return _round(numerator * Decimal(100) / denominator)

    applicable_weight = sum(Decimal(str(row["weight"])) for row in rows if row["raw_points"] is not None)
    for row in rows:
        row["effective_weight"] = (_round(Decimal(str(row["weight"])) * 100 / applicable_weight)
                                   if row["raw_points"] is not None and applicable_weight else None)

    sections = {section["id"]: subtotal([row for row in rows if row["section"] == section["id"]])
                for section in checklist["sections"]}
    flags = {c["id"]: answers[c["id"]]["answer"] == "yes"
             for c in checklist["criteria"] if c["type"] == "flag"}
    return {"total": subtotal(rows), "sections": sections, "flags": flags, "rows": rows}
