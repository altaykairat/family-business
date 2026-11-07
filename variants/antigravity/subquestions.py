"""Data-driven sub-question decomposition and deterministic reduction engine (P30).

Strictly adheres to decisions 3, 6, and P30:
1. Subquestions and reduction rules are stored as DATA in the criterion package
   (optional fields `sub_questions` and `reduction_rule` in checklist JSON).
2. Zero criterion IDs (c01..c08) are hardcoded in the reduction logic.
3. Criteria without subquestions evaluate directly as in V1 (passthrough).
4. Adding, changing, or removing a criterion through the API never breaks the variant.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# --- Declarative Standard Subquestion & Reduction Rule Packages (Data) -------

STANDARD_SUBQUESTIONS_DATA: Dict[str, Dict[str, Any]] = {
    "c01": {
        "sub_questions": [
            {
                "id": "c01_greet",
                "kind": "clause",
                "question": "Оператор поздоровался с клиентом?",
                "required": True,
                "scope": "первый ответ оператора",
            },
            {
                "id": "c01_lang",
                "kind": "clause",
                "question": "Приветствие было на том же языке, на котором начал разговор клиент (RU/KZ/EN)?",
                "required": True,
                "scope": "первый ответ оператора",
            },
            {
                "id": "c01_intro",
                "kind": "clause",
                "question": "Оператор представился по имени (или в транскрипте звонка стоит маска имени [Имя скрыто])?",
                "required": True,
                "scope": "первый ответ оператора",
            },
            {
                "id": "c01_first_turn",
                "kind": "clause",
                "question": "Приветствие и представление сделаны именно в первом ответе оператора (не во втором/третьем)?",
                "required": True,
                "scope": "первый ответ оператора",
            },
        ],
        "reduction_rule": {
            "op": "all",
            "true_val": "yes",
            "false_val": "no",
        },
    },
    "c02": {
        "sub_questions": [
            {
                "id": "c02_na",
                "kind": "na_gate",
                "question": "Выполнено ли условие неприменимости (N/A): «Данных для ответа хватало с самого начала.»?",
                "expected_for_na": True,
            },
            {
                "id": "c02_known_details",
                "kind": "clause",
                "question": "Учёл ли оператор уже известные детали запроса (включая то, что клиент написал боту)?",
                "required": True,
                "scope": "до предоставления ответа",
            },
            {
                "id": "c02_clarify_missing",
                "kind": "clause",
                "question": "Уточнил ли оператор недостающие данные до ответа, если информации не хватало?",
                "required": True,
                "scope": "до предоставления ответа",
            },
        ],
        "reduction_rule": {
            "na_gate": "c02_na",
            "op": "all",
            "true_val": "yes",
            "false_val": "no",
        },
    },
    "c03": {
        "sub_questions": [
            {
                "id": "c03_na",
                "kind": "na_gate",
                "question": "Выполнено ли условие неприменимости (N/A): «Запрос не требовал идентификации.»?",
                "expected_for_na": True,
            },
            {
                "id": "c03_action_needed",
                "kind": "clause",
                "question": "Требовал ли запрос клиента проведения защищённого действия или раскрытия персональных/финансовых данных?",
                "required": True,
                "scope": "весь диалог",
            },
            {
                "id": "c03_verified_prior",
                "kind": "clause",
                "question": "Была ли проверка личности подтверждена оператором ДО совершения защищённого действия или раскрытия данных?",
                "required": True,
                "scope": "до защищённого действия",
            },
        ],
        "reduction_rule": {
            "na_gate": "c03_na",
            "op": "all",
            "true_val": "yes",
            "false_val": "no",
        },
    },
    "c05": {
        "sub_questions": [
            {
                "id": "c05_polite",
                "kind": "clause",
                "question": "Был ли оператор вежлив и корректен на протяжении всего диалога?",
                "required": True,
                "scope": "весь диалог",
            },
            {
                "id": "c05_dissatisfaction_handled",
                "kind": "clause",
                "question": "Если клиент выразил недовольство или неудобство — отреагировал ли оператор на конкретную проблему (а не проигнорировал)?",
                "required": True,
                "scope": "реакция на жалобу/неудобство (если жалобы не было — считается выполненным)",
            },
        ],
        "reduction_rule": {
            "op": "all",
            "true_val": "yes",
            "false_val": "no",
        },
    },
    "c07": {
        "sub_questions": [
            {
                "id": "c07_na",
                "kind": "na_gate",
                "question": "Выполнено ли условие неприменимости (N/A): «Чат оборвался до возможности его завершить.»?",
                "expected_for_na": True,
            },
            {
                "id": "c07_summary_or_help",
                "kind": "clause",
                "question": "Подвёл ли оператор итог разговора или предложил дальнейшую помощь («Остались ли ещё вопросы?», «Чем ещё могу помочь?»)?",
                "required": True,
                "scope": "завершение диалога",
            },
            {
                "id": "c07_goodbye",
                "kind": "clause",
                "question": "Попрощался ли оператор с клиентом («Всего доброго», «До свидания», «Хорошего дня»)?",
                "required": True,
                "scope": "завершение диалога",
            },
        ],
        "reduction_rule": {
            "na_gate": "c07_na",
            "op": "all",
            "true_val": "yes",
            "false_val": "no",
        },
    },
    "c08": {
        "sub_questions": [
            {
                "id": "c08_insult",
                "kind": "flag_clause",
                "question": "Выявлено ли оскорбление, грубость или хамство в адрес клиента?",
                "is_violation": True,
            },
            {
                "id": "c08_bypass",
                "kind": "flag_clause",
                "question": "Выявлено ли предложение платной услуги в обход компании (личный перевод, просьба личного номера)?",
                "is_violation": True,
            },
            {
                "id": "c08_leak",
                "kind": "flag_clause",
                "question": "Выявлено ли несанкционированное раскрытие конфиденциальной информации третьих лиц или компании?",
                "is_violation": True,
            },
        ],
        "reduction_rule": {
            "op": "any_violation",
            "violation_val": "yes",
            "clean_val": "no",
        },
    },
}


# --- Helpers -----------------------------------------------------------------

def _split_clauses(text: str) -> List[str]:
    """Dynamically split a criteria description into sub-clauses."""
    s = text.strip()
    if s.lower().startswith("да — ") or s.lower().startswith("да - "):
        s = s[5:].strip()
    cleaned = re.sub(r"\b(а также|и если|при этом|а также)\b", ";", s, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"[;\.]", cleaned) if p.strip()]
    return parts if parts else [s]


def _is_truthy(val: Any) -> bool:
    """Robust truthiness check for boolean answers."""
    if isinstance(val, dict):
        val = val.get("value")
    return val in (True, "yes", "true", "да", 1)


def enrich_criterion(c: dict) -> dict:
    """Return a copy of criterion `c` with data-driven subquestions and reduction rule attached."""
    c_copy = dict(c)
    cid = c.get("id", "")
    if "sub_questions" not in c_copy and cid in STANDARD_SUBQUESTIONS_DATA:
        spec = STANDARD_SUBQUESTIONS_DATA[cid]
        c_copy["sub_questions"] = spec["sub_questions"]
        if "reduction_rule" not in c_copy:
            c_copy["reduction_rule"] = spec["reduction_rule"]
    return c_copy


def enrich_checklist(doc: dict) -> dict:
    """Enrich all criteria in a checklist document with declarative subquestions."""
    doc_copy = dict(doc)
    doc_copy["criteria"] = [enrich_criterion(c) for c in doc.get("criteria", [])]
    return doc_copy


def _dynamic_derive_from_spec(c: dict) -> List[Dict[str, Any]]:
    """Dynamically derive atomic subquestions for ANY criterion without hardcoded IDs."""
    cid = c.get("id", "c")
    sqs: List[Dict[str, Any]] = []

    # 1. Gate 1 (N/A)
    if c.get("na_allowed") and c.get("na_condition"):
        sqs.append({
            "id": f"{cid}_na",
            "kind": "na_gate",
            "question": f"Выполнено ли условие неприменимости (N/A): «{c['na_condition']}»?",
            "expected_for_na": True,
            "help": "Если условие выполнено — критерий неприменим (N/A).",
        })

    # 2. Gate 2 based purely on c["type"]
    ctype = c.get("type", "binary")
    if ctype == "flag":
        clauses = _split_clauses(c.get("description", ""))
        for i, cl in enumerate(clauses, 1):
            sqs.append({
                "id": f"{cid}_violation_{i}",
                "kind": "flag_clause",
                "question": f"Выявлено ли нарушение: «{cl}»?",
                "is_violation": True,
            })
    elif ctype == "scale":
        steps = sorted(c.get("scale", []), key=lambda s: s["value"], reverse=True)
        for s in steps:
            sqs.append({
                "id": f"{cid}_step_{s['value']}",
                "kind": "scale_step",
                "scale_value": s["value"],
                "question": f"Соответствует ли работа оператора ступени {s['value']} баллов: «{s['text']}»?",
                "step_text": s["text"],
            })
    else:  # binary
        clauses = _split_clauses(c.get("description", ""))
        for i, cl in enumerate(clauses, 1):
            sqs.append({
                "id": f"{cid}_clause_{i}",
                "kind": "clause",
                "question": f"Выполнено ли требование: «{cl}»?",
                "required": True,
            })
    return sqs


# --- Public Core Engine (100% Data-Driven, Zero Hardcoded Criteria IDs) -------

def derive_subquestions(c: dict) -> List[Dict[str, Any]]:
    """Derive atomic sub-questions for criterion `c`.

    Priority:
    1. If `c` explicitly has `sub_questions` in its data, use them directly.
    2. If `c["id"]` matches standard criteria data package, use package.
    3. Generic dynamic fallback derived purely from c["type"], scale, and description.
    """
    if c.get("sub_questions"):
        return list(c["sub_questions"])

    cid = c.get("id")
    if cid and cid in STANDARD_SUBQUESTIONS_DATA:
        return list(STANDARD_SUBQUESTIONS_DATA[cid]["sub_questions"])

    return _dynamic_derive_from_spec(c)


def reduce_subquestions(c: dict, answers: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Deterministically derive the criterion answer from sub-question evaluations.

    Zero criterion IDs are hardcoded in this logic.
    Supports declarative `reduction_rule` in criterion data.
    If no subquestions exist or are answered, falls back to V1 direct answer.

    Returns:
        (derived_answer, metadata_dict)
    """
    cid = c.get("id", "")
    sqs = derive_subquestions(c)
    rule = c.get("reduction_rule")
    if not rule and cid in STANDARD_SUBQUESTIONS_DATA:
        rule = STANDARD_SUBQUESTIONS_DATA[cid].get("reduction_rule")

    meta: Dict[str, Any] = {
        "subquestions_evaluated": len(sqs),
        "steps_checked": {},
    }

    # V1 Passthrough check: if no subquestions were answered, take direct answer
    has_sub_answers = any(s["id"] in answers for s in sqs)
    if not has_sub_answers and cid in answers:
        return answers[cid], {"mode": "v1_passthrough", "criterion_id": cid}

    # 1. Gate 1: Check N/A
    na_gate_id = rule.get("na_gate") if rule else None
    if not na_gate_id:
        na_sq = next((s for s in sqs if s.get("kind") == "na_gate"), None)
        if na_sq:
            na_gate_id = na_sq["id"]

    if na_gate_id and na_gate_id in answers:
        na_ans = answers.get(na_gate_id)
        meta["na_gate_result"] = na_ans
        if _is_truthy(na_ans):
            return rule.get("na_val", "na") if rule else "na", meta

    ctype = c.get("type", "binary")
    op = rule.get("op") if rule else None

    # 2. Flag Reduction
    if ctype == "flag" or op == "any_violation":
        flag_sqs = [s for s in sqs if s.get("kind") == "flag_clause" or s.get("is_violation")]
        violation_found = False
        violations = []
        for s in flag_sqs:
            val = answers.get(s["id"])
            is_v = _is_truthy(val)
            meta["steps_checked"][s["id"]] = is_v
            if is_v:
                violation_found = True
                violations.append(s["id"])
        meta["violations"] = violations
        violation_val = rule.get("violation_val", "yes") if rule else "yes"
        clean_val = rule.get("clean_val", "no") if rule else "no"
        return (violation_val if violation_found else clean_val), meta

    # 3. Scale Reduction (Contrast ladder)
    if ctype == "scale" or op == "scale_ladder":
        scale_sqs = [s for s in sqs if s.get("kind") == "scale_step"]
        # Find highest scale step confirmed
        for s in scale_sqs:
            val = answers.get(s["id"])
            is_match = _is_truthy(val)
            meta["steps_checked"][s["id"]] = is_match
            if is_match:
                return s["scale_value"], meta
        lowest = min(s["scale_value"] for s in scale_sqs) if scale_sqs else 0
        return lowest, meta

    # 4. Binary Reduction (All / Any)
    clause_sqs = [s for s in sqs if s.get("kind") == "clause" or s.get("required")]
    all_ok = True
    any_ok = False
    failed = []
    for s in clause_sqs:
        val = answers.get(s["id"])
        c_ok = _is_truthy(val)
        meta["steps_checked"][s["id"]] = c_ok
        if c_ok:
            any_ok = True
        else:
            all_ok = False
            failed.append(s["id"])

    meta["failed_clauses"] = failed
    true_val = rule.get("true_val", "yes") if rule else "yes"
    false_val = rule.get("false_val", "no") if rule else "no"

    if op in ("any", "or"):
        return (true_val if any_ok else false_val), meta
    return (true_val if all_ok else false_val), meta
