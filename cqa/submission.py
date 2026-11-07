"""S8.4. Export to the organizers' format (results/<dev|prod>/results.json, schema 1.0) and a pre-push check.

Our criterion ids → organizers' codes live in a profile in config/platform.json (`submission.profiles`), never in
code or in the checklist file. Answers: yes → true, no → false, na → null, scale step → number. Scores are
recomputed by the platform formula (S7). A conversation without a usable result is exported as an error, never as
a zero. Source: published evaluations with reviewer decisions on top (default) or any `run:agent`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import checklist as ck
from . import common, events, scoring, sources, state
from . import conversation as cv
from .common import CQAError, rel, save_json

SETS = ("dev", "prod")


def profile(name: Optional[str] = None) -> dict:
    cfg = common.setting("submission", {}) or {}
    name = name or cfg.get("default_profile", "default")
    prof = (cfg.get("profiles") or {}).get(name)
    if not prof:
        raise CQAError(f"export profile {name!r} not in config/platform.json → submission.profiles", "not_found")
    return {"name": name, **prof}


def _mapping(doc: dict, prof: dict) -> dict:
    """{our criterion id: organizers' code}; every active criterion must be mapped and every code covered."""
    cmap = prof["criteria_map"]
    active = [c["id"] for c in ck.active_criteria(doc)]
    unmapped = [c for c in active if c not in cmap]
    missing = [code for cid, code in cmap.items() if cid not in active]
    if unmapped or missing:
        raise CQAError(
            f"чек-лист {ck.ref_of(doc)} не соответствует профилю экспорта {prof['name']!r}: "
            + (f"без кода организаторов: {unmapped}; " if unmapped else "")
            + (f"нет критериев для кодов: {missing}. " if missing else "")
            + "Формат организаторов — ровно 8 их критериев; экспорт по изменённому чек-листу невозможен, "
              "добавьте профиль или экспортируйте по версии с полным соответствием.", "conflict")
    return {cid: cmap[cid] for cid in active}


def _value(answer):
    if answer == "yes":
        return True
    if answer == "no":
        return False
    if answer == "na" or answer is None:
        return None
    return answer if isinstance(answer, (int, float)) else None


def _turns(item: dict) -> list:
    ok = [e.get("idx") for e, chk in zip(item.get("evidence") or [], item.get("evidence_checks") or [{}] * 99)
          if isinstance(e.get("idx"), int) and chk.get("exists", True) is not False]
    if not ok and isinstance(item.get("checked_range"), list):
        ok = [i for i in item["checked_range"] if isinstance(i, int)]
    return sorted(set(ok))


def _record(conv_id: str, data: Optional[dict], answers: Optional[dict], doc: dict, cmap: dict,
            criteria_codes: list) -> dict:
    if data is None:
        return {"conversation_id": conv_id, "status": "error", "criteria": [], "score": None,
                "error": "Нет результата оценки для диалога."}
    missing = [cid for cid in cmap if cid not in (answers or {})]
    if missing:
        return {"conversation_id": conv_id, "status": "error", "criteria": [], "score": None,
                "error": f"Нет валидного ответа по критериям {', '.join(missing)} "
                         f"(статус проверки: {data.get('status')})."}
    sc = scoring.score(doc, answers)
    if sc["total"] is None:
        return {"conversation_id": conv_id, "status": "error", "criteria": [], "score": None,
                "error": "Итог не вычислен: нет применимых критериев."}
    items = data.get("criteria") or {}
    crit = []
    for cid, code in cmap.items():
        item = items.get(cid) or {}
        crit.append({"criterion_id": code, "value": _value(answers[cid]),
                     "rationale": (item.get("explanation") or item.get("reason") or "").strip(),
                     "evidence_turns": _turns(item)})
    order = {c: n for n, c in enumerate(criteria_codes)}
    crit.sort(key=lambda c: order.get(c["criterion_id"], 99))
    flag_code = next((code for cid, code in cmap.items()
                      if next(c for c in doc["criteria"] if c["id"] == cid)["type"] == "flag"), None)
    flag = next((c["value"] for c in crit if c["criterion_id"] == flag_code), None)
    return {"conversation_id": conv_id, "status": "ok", "criteria": crit,
            "score": {"final_score": round(float(sc["total"]), 2), "critical_flag": bool(flag)}}


def build(dataset: str = "dev", source: Optional[str] = None, profile_name: Optional[str] = None,
          until: Optional[str] = None, ids: Optional[list] = None) -> dict:
    """Assemble the organizers' document. `source=None` → published evaluations + reviewer decisions."""
    prof = profile(profile_name)
    conv_ids = ids or cv.list_ids(dataset)
    per_conv, doc_ref, provenance = {}, None, {}
    if source:
        src, results = sources.load_results(source)
        doc_ref = sources.checklist_ref(src, results)
        for conv, data in results.items():
            per_conv[conv] = (data, data.get("answers") or {})
            provenance[conv] = {"source": source}
    else:
        st = state.load(until)
        for conv, eid in st.published.items():
            ev = st.evaluations[eid]
            data = events.get_blob(ev["blob"]) if ev.get("blob") else {"criteria": {}}
            fin = st.final(eid)
            data = dict(data)
            crit = dict(data.get("criteria") or {})
            for cid, dec_id in fin["decisions"].items():  # a reviewer's reason replaces the model's explanation
                d = st.decisions[dec_id]
                if d["action"] == "change":
                    crit[cid] = {**(crit.get(cid) or {}), "explanation": f"Решение проверяющего: {d['reason']}"}
            data["criteria"] = crit
            per_conv[conv] = (data, fin["final"]["answers"])
            doc_ref = doc_ref or ev["checklist"]
            provenance[conv] = {"evaluation_id": eid, "decisions": list(fin["decisions"].values())}
    if not doc_ref:
        raise CQAError("нечего экспортировать: нет опубликованных оценок (eval.publish) и не указан source",
                       "not_found")
    doc = ck.load(doc_ref.replace("@v", "@"))
    cmap = _mapping(doc, prof)
    codes = list(prof["criteria_map"].values())
    results = [_record(c, *(per_conv.get(c) or (None, None)), doc, cmap, codes) for c in conv_ids]
    out = {"schema_version": "1.0", "team_name": prof["team_name"], "model": prof["model"], "results": results}
    return {"document": out, "checklist": ck.ref_of(doc), "provenance": provenance}


def export(set_name: str = "dev", dataset: Optional[str] = None, source: Optional[str] = None,
           profile_name: Optional[str] = None, actor: str = "system", path: Optional[str] = None,
           model_name: Optional[str] = None, model_mode: Optional[str] = None) -> dict:
    if set_name not in SETS:
        raise CQAError(f"set must be one of {SETS}")
    res = build(dataset or set_name, source, profile_name)
    doc = res["document"]
    if model_name:
        doc["model"] = {**doc["model"], "name": model_name}
    if model_mode:
        doc["model"] = {**doc["model"], "mode": model_mode}
    target = Path(path) if path else common.WORKSPACE / "results" / set_name / "results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = check(str(target), set_name, profile_name)
    blob = events.put_blob(doc)
    common.log_event("results_exported", actor=actor, set=set_name, source=source or "published",
                     checklist=res["checklist"], path=rel(target), blob=blob, ok=report["ok"],
                     counts=report["counts"])
    save_json(target.with_name("provenance.json"), {"source": source or "published", "checklist": res["checklist"],
                                                   "blob": blob, "conversations": res["provenance"]})
    return {"path": rel(target), "checklist": res["checklist"], "check": report}


def _rescore(crit: list, prof: dict) -> Optional[float]:
    """final_score recomputed from the values with the checklist the profile was made for (sanity check)."""
    try:
        doc = ck.load(prof.get("checklist", "default@1"))
    except CQAError:
        return None
    back = {code: cid for cid, code in prof["criteria_map"].items()}
    answers = {}
    for c in crit:
        v = c.get("value")
        cid = back.get(c.get("criterion_id"))
        if cid:
            answers[cid] = "yes" if v is True else "no" if v is False else "na" if v is None else v
    try:
        total = scoring.score(doc, answers)["total"]
    except Exception:  # noqa: BLE001 — a malformed value is reported by the type check above
        return None
    return None if total is None else round(float(total), 2)


def check(path: str, set_name: Optional[str] = None, profile_name: Optional[str] = None,
          expected: Optional[int] = None) -> dict:
    """Pre-push check: UTF-8 JSON, schema 1.0, unique ids, 8 criteria exactly once with valid value types,
    final_score 0..100 with 2 decimals, critical_flag = value of `critical`, error records well-formed, count."""
    prof = profile(profile_name)
    types = prof["value_types"]
    errors, warnings = [], []
    p = Path(path)
    try:
        doc = json.loads(p.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return {"ok": False, "errors": [f"файл не читается как UTF-8 JSON: {e}"], "warnings": [], "counts": {}}
    if doc.get("schema_version") != "1.0":
        errors.append(f"schema_version = {doc.get('schema_version')!r}, нужно \"1.0\"")
    if not (doc.get("team_name") or "").strip():
        errors.append("пустой team_name")
    model = doc.get("model") or {}
    if model.get("mode") not in ("local", "external_api", "hybrid") or not model.get("name"):
        errors.append(f"model: mode ∈ local/external_api/hybrid и непустой name — сейчас {model}")
    results = doc.get("results")
    if not isinstance(results, list):
        return {"ok": False, "errors": errors + ["results — не массив"], "warnings": warnings, "counts": {}}
    ids = [r.get("conversation_id") for r in results]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errors.append(f"повторяющиеся conversation_id: {dup}")
    if expected is None and set_name:
        expected = prof.get("expected_counts", {}).get(set_name)
    if expected is not None and len(results) != expected:
        errors.append(f"записей {len(results)}, ожидается {expected}")
    if set_name == "dev":
        known = set(cv.list_ids("dev"))
        unknown = [i for i in ids if i not in known]
        if unknown:
            errors.append(f"ID не из набора dev: {unknown[:5]}")
    n_ok = n_err = 0
    for r in results:
        cid = r.get("conversation_id")
        st = r.get("status")
        if st == "error":
            n_err += 1
            if r.get("criteria") != [] or r.get("score") is not None or not r.get("error"):
                errors.append(f"{cid}: ошибка оформляется как criteria=[], score=null и причина в error")
            continue
        if st != "ok":
            errors.append(f"{cid}: status {st!r}, нужно ok или error")
            continue
        n_ok += 1
        crit = r.get("criteria") or []
        codes = [c.get("criterion_id") for c in crit]
        if sorted(codes) != sorted(types):
            errors.append(f"{cid}: критерии {codes}, нужно ровно по разу {list(types)}")
        for c in crit:
            allowed = types.get(c.get("criterion_id"))
            if allowed is None:
                continue
            v = c.get("value")
            ok = (v is None and None in allowed) or (isinstance(v, bool) and "bool" in allowed) or \
                 (not isinstance(v, bool) and isinstance(v, (int, float)) and v in allowed)
            if not ok:
                errors.append(f"{cid}/{c['criterion_id']}: значение {v!r} недопустимо ({allowed})")
            turns = c.get("evidence_turns")
            if not isinstance(turns, list) or not all(isinstance(t, int) and t >= 1 for t in turns):
                errors.append(f"{cid}/{c['criterion_id']}: evidence_turns — список номеров реплик с 1")
            elif not turns:
                warnings.append(f"{cid}/{c['criterion_id']}: нет evidence_turns")
            if not (c.get("rationale") or "").strip():
                warnings.append(f"{cid}/{c['criterion_id']}: пустой rationale")
        sc = r.get("score") or {}
        fs = sc.get("final_score")
        if not isinstance(fs, (int, float)) or isinstance(fs, bool) or not 0 <= fs <= 100 or round(fs, 2) != fs:
            errors.append(f"{cid}: final_score {fs!r} — число 0..100 с двумя знаками")
        recomputed = _rescore(crit, prof)
        if recomputed is not None and isinstance(fs, (int, float)) and abs(recomputed - fs) > 0.011:
            warnings.append(f"{cid}: final_score {fs} ≠ пересчёт по формуле {recomputed}")
        crit_v = next((c.get("value") for c in crit if c.get("criterion_id") == prof.get("flag_code", "critical")), None)
        if sc.get("critical_flag") is not bool(crit_v) or not isinstance(sc.get("critical_flag"), bool):
            errors.append(f"{cid}: critical_flag должен совпадать с value критерия critical")
    return {"ok": not errors, "errors": errors[:50], "errors_total": len(errors), "warnings": warnings[:30],
            "warnings_total": len(warnings), "counts": {"records": len(results), "ok": n_ok, "error": n_err},
            "path": rel(p) if p.is_absolute() else str(p)}
