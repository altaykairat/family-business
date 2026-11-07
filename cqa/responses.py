"""S5. Parse raw model answers (pasted text) and validate them against the checklist and dialog."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Optional

from .checklist import active_criteria

YES = {"yes", "да", "y", "true", "иә", "ia"}
NO = {"no", "нет", "n", "false", "жоқ"}
NA = {"na", "n/a", "н/а", "не применимо", "неприменимо", "not applicable"}


def parse(raw: str) -> tuple:
    """Extract one JSON object from pasted text. Returns (data, error)."""
    text = raw.strip().lstrip("﻿")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        return None, "JSON не найден"
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            return json.loads(attempt), None
        except json.JSONDecodeError as e:
            err = f"невалидный JSON: {e}"
    return None, err


def normalize_answer(raw, c: dict) -> tuple:
    """Returns (normalized answer or None, issue or None)."""
    if raw is None:
        return None, "ответ отсутствует"
    s = str(raw).strip().lower().strip(".")
    if s in NA:
        return ("na", None) if c.get("na_allowed") else (None, f"N/A не разрешён для {c['id']}")
    if c["type"] in ("binary", "flag"):
        if s in YES:
            return "yes", None
        if s in NO:
            return "no", None
        return None, f"недопустимый ответ {raw!r}"
    if c["type"] == "scale":
        try:
            v = float(s.replace(",", "."))
        except ValueError:
            return None, f"нечисловой ответ {raw!r} для шкалы"
        steps = [s_["value"] for s_ in c["scale"]]
        if v in steps:
            return (int(v) if float(v).is_integer() else v), None
        nearest = min(steps, key=lambda x: abs(x - v))
        return nearest, f"значение {v} не ступень шкалы — округлено до {nearest}"
    return None, f"неизвестный тип {c['type']}"


def _norm_text(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„`]", "", s)
    return " ".join(s.split()).strip(" .,!?:;—-")


def quote_coverage(quote: str, text: str) -> float:
    """Share of the quote found in the turn text (1.0 = exact substring after normalization)."""
    q, t = _norm_text(quote or ""), _norm_text(text or "")
    if not q:
        return 0.0
    if q in t:
        return 1.0
    blocks = SequenceMatcher(None, q, t, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(q)


def _flag(value) -> Optional[bool]:
    """Template v2 field `insufficient_data`: bool, tolerant to "true"/"false"/0/1; None when unreadable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "да", "нет", "yes", "no"):
        return value.strip().lower() in ("true", "да", "yes")
    return None


def validate(conv: dict, doc: dict, data: dict, quote_threshold: float = 0.9,
             expected_ids: Optional[list] = None) -> dict:
    """Checks answers, N/A, scale steps, evidence idx/quotes. Never changes the model's reasoning."""
    turns = {t["idx"]: t for t in conv["transcript"]}
    crit = {c["id"]: c for c in active_criteria(doc)}
    expected = expected_ids or list(crit)
    issues, answers, criteria_out = [], {}, {}

    items = {}
    for item in data.get("criteria", []) or []:
        cid = item.get("criterion_id")
        if cid in items:
            issues.append({"level": "warn", "criterion_id": cid, "msg": "критерий повторяется — взят первый"})
            continue
        items[cid] = item

    for cid in expected:
        c = crit.get(cid)
        item = items.get(cid)
        if item is None:
            issues.append({"level": "error", "criterion_id": cid, "msg": "критерий отсутствует в ответе"})
            continue
        ans, problem = normalize_answer(item.get("answer"), c)
        if problem:
            issues.append({"level": "error" if ans is None else "warn", "criterion_id": cid, "msg": problem})
        if ans is not None:
            answers[cid] = ans

        ev_checks = []
        for ev in item.get("evidence") or []:
            idx = ev.get("idx")
            turn = turns.get(idx)
            if turn is None:
                issues.append({"level": "error", "criterion_id": cid, "msg": f"реплики #{idx} нет в диалоге"})
                ev_checks.append({"idx": idx, "exists": False})
                continue
            cov = round(quote_coverage(ev.get("quote", ""), turn["text"]), 3)
            ok = cov >= quote_threshold
            if not ok:
                issues.append({"level": "warn", "criterion_id": cid,
                               "msg": f"цитата не совпадает с репликой #{idx} (покрытие {cov})"})
            ev_checks.append({"idx": idx, "exists": True, "speaker": turn["speaker"], "quote_coverage": cov,
                              "quote_ok": ok})
        if not item.get("evidence") and ans not in (None, "na"):
            issues.append({"level": "warn", "criterion_id": cid, "msg": "нет доказательства"})
        insufficient = _flag(item.get("insufficient_data"))
        if insufficient is None and item.get("insufficient_data") is not None:
            issues.append({"level": "warn", "criterion_id": cid,
                           "msg": f"insufficient_data не bool: {item.get('insufficient_data')!r}"})
        criteria_out[cid] = {**item, "answer_normalized": ans, "evidence_checks": ev_checks,
                             "insufficient_data": bool(insufficient)}

    for cid in set(items) - set(expected):
        issues.append({"level": "warn", "criterion_id": cid, "msg": "лишний критерий в ответе — проигнорирован"})

    status = "error" if any(i["level"] == "error" for i in issues) else ("warn" if issues else "ok")
    return {"status": status, "issues": issues, "answers": answers, "criteria": criteria_out,
            "insufficient_data": sorted(c for c, it in criteria_out.items() if it.get("insufficient_data")),
            "dialog_facts": data.get("dialog_facts"), "model_total_estimate": data.get("model_total_estimate"),
            "overall_comment": data.get("overall_comment"), "meta": data.get("meta")}
