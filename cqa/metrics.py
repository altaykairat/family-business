"""S9. Accuracy metrics against ground truth and head-to-head comparison of runs and variants (M.1–M.13,
ST.1–ST.3). Works on any source in the base result format, so the base pipeline, Antigravity's and Codex's
variants and the agent ensemble are compared on the same labels.

Totals are recomputed by the platform's scoring from the answers (never taken from the model), with the
checklist version of the ground truth; metrics use only criteria present in both (CH.13).
"""

from __future__ import annotations

import math
from collections import Counter
from itertools import combinations
from typing import Optional

from . import checklist as ck
from . import common, events, groundtruth, scoring, sources, stats, tokens
from . import conversation as cv
from .common import CQAError, save_json


def key(answer) -> Optional[str]:
    """Comparable form of an answer: 'yes' / 'no' / 'na' / '15'."""
    if answer is None:
        return None
    if isinstance(answer, float) and answer.is_integer():
        answer = int(answer)
    return str(answer).strip().lower()


def _num(answer) -> Optional[float]:
    try:
        return float(answer)
    except (TypeError, ValueError):
        return None


def evaluate(gold: dict, spec: str, doc: dict, cfg: Optional[dict] = None, results: Optional[dict] = None) -> dict:
    """All metrics of one prediction source against one gold set."""
    cfg = cfg or common.load_config()
    window = int(common.setting("metrics.evidence_window", 1))
    tol = float(common.setting("metrics.total_tolerance", 5))
    if results is None:
        src, results = sources.load_results(spec)
    else:
        src = {"label": spec, "agent": spec, "manifest": None}
    crit = [c for c in ck.active_criteria(doc)]
    crit_ids = [c["id"] for c in crit]
    by_id = {c["id"]: c for c in crit}
    items = gold["items"]
    conv_ids = sorted(items)
    covered = [c for c in conv_ids if c in results]

    decisions = []  # (conv, crit, gold_key, pred_key, correct)
    per_crit = {cid: {"pairs": [], "conf": Counter(), "abs_err": []} for cid in crit_ids}
    ev_total = ev_exists = ev_quote = 0
    ev_match = ev_match_exact = ev_considered = 0
    invalid = 0
    missing_answers = 0
    for conv in covered:
        r = results[conv]
        if r.get("status") == "error":
            invalid += 1
        answers = r.get("answers") or {}
        for cid, lab in items[conv].items():
            if cid not in by_id:
                continue
            g, p = key(lab["answer"]), key(answers.get(cid))
            if p is None:
                missing_answers += 1
            ok = g == p
            decisions.append((conv, cid, g, p, ok))
            pc = per_crit[cid]
            pc["pairs"].append((g, p))
            pc["conf"][(g, p)] += 1
            if by_id[cid]["type"] == "scale" and _num(g) is not None and _num(p) is not None:
                pc["abs_err"].append(abs(_num(g) - _num(p)))
            item = (r.get("criteria") or {}).get(cid, {})
            for chk in item.get("evidence_checks") or []:
                ev_total += 1
                ev_exists += bool(chk.get("exists"))
                ev_quote += bool(chk.get("quote_ok"))
            gidx = [i for i in lab.get("evidence_idx") or [] if isinstance(i, int)]
            pidx = [e.get("idx") for e in item.get("evidence") or [] if isinstance(e.get("idx"), int)]
            if gidx and g not in ("na",):
                ev_considered += 1
                if any(abs(a - b) <= window for a in pidx for b in gidx):
                    ev_match += 1
                if set(pidx) & set(gidx):
                    ev_match_exact += 1

    criteria_out = {}
    for c in crit:
        pc = per_crit[c["id"]]
        n = len(pc["pairs"])
        correct = sum(a == b for a, b in pc["pairs"])
        row = {"title": c["title"], "type": c["type"], **stats.proportion(correct, n), "kappa": stats.kappa(pc["pairs"]),
               "confusion": {f"{a}→{b}": v for (a, b), v in sorted(pc["conf"].items(), key=lambda x: str(x))},
               "no_na": {"gold_no_pred_na": pc["conf"][("no", "na")], "gold_na_pred_no": pc["conf"][("na", "no")]}}
        if c["type"] == "scale":
            steps = sorted(s["value"] for s in c["scale"])
            gaps = [b - a for a, b in zip(steps, steps[1:])]
            step = min(gaps) if gaps else 1
            row["mae_points_on_scale"] = stats.mean(pc["abs_err"])
            row["within_one_step"] = (round(sum(e <= max(gaps or [step]) for e in pc["abs_err"]) / len(pc["abs_err"]), 4)
                                      if pc["abs_err"] else None)
        if c["type"] == "flag":
            tp = pc["conf"][("yes", "yes")]
            fp = sum(v for (a, b), v in pc["conf"].items() if b == "yes" and a != "yes")
            fn = sum(v for (a, b), v in pc["conf"].items() if a == "yes" and b != "yes")
            prec = tp / (tp + fp) if tp + fp else None
            rec = tp / (tp + fn) if tp + fn else None
            row["flag"] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec and round(prec, 4),
                           "recall": rec and round(rec, 4),
                           "f1": round(2 * prec * rec / (prec + rec), 4) if prec and rec else None}
        criteria_out[c["id"]] = row
    accs = [v["share"] for v in criteria_out.values() if v["share"] is not None]
    macro = round(sum(accs) / len(accs), 4) if accs else None
    micro = stats.proportion(sum(d[4] for d in decisions), len(decisions))

    # totals (M.5, M.6) — only for conversations fully labelled in gold
    total_rows = []
    for conv in covered:
        labels = items[conv]
        if not set(crit_ids) <= set(labels):
            continue
        g_total = scoring.score(doc, {k: v["answer"] for k, v in labels.items() if k in by_id})["total"]
        p_score = scoring.score(doc, results[conv].get("answers") or {})
        total_rows.append({"conversation_id": conv, "gold": g_total, "pred": p_score["total"],
                           "complete": p_score["complete"]})
    errs = [(t["pred"] - t["gold"]) for t in total_rows if t["gold"] is not None and t["pred"] is not None]
    both_none = sum(1 for t in total_rows if t["gold"] is None and t["pred"] is None)
    totals = {"n": len(total_rows), "mae": stats.mean([abs(e) for e in errs]),
              "rmse": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 4) if errs else None,
              "bias": stats.mean(errs),
              "within_tolerance": stats.proportion(sum(abs(e) <= tol for e in errs) + both_none, len(total_rows)),
              "tolerance": tol, "rows": total_rows}

    # bootstrap over conversations (ST.1)
    by_conv: dict = {}
    for d in decisions:
        by_conv.setdefault(d[0], []).append(d)
    conv_list = list(by_conv.values())

    def macro_of(sample):
        per: dict = {}
        for group in sample:
            for _, cid, _, _, ok in group:
                per.setdefault(cid, []).append(ok)
        vals = [sum(v) / len(v) for v in per.values() if v]
        return sum(vals) / len(vals) if vals else None

    samples = int(common.setting("metrics.bootstrap_samples", 1000))
    seed = int(common.setting("metrics.bootstrap_seed", 7))
    macro_ci = stats.bootstrap(conv_list, macro_of, samples, seed)
    mae_ci = stats.bootstrap([abs(e) for e in errs], lambda s: sum(s) / len(s), samples, seed) if errs else None

    # breakdown by language and channel (ST.3)
    breakdown: dict = {}
    for conv, group in by_conv.items():
        try:
            rec = cv.load((src.get("manifest") or {}).get("dataset") or "dev", conv)
            keys = [f"language:{rec['language']}", f"channel:{rec['channel']}"]
        except CQAError:
            keys = ["unknown"]
        for k in keys:
            b = breakdown.setdefault(k, [0, 0])
            b[0] += sum(d[4] for d in group)
            b[1] += len(group)
    breakdown = {k: stats.proportion(a, n) for k, (a, n) in sorted(breakdown.items())}

    # cost (M.12)
    usage = [u for c in covered for u in results[c].get("usage") or []]
    tin = sum(u.get("tokens_in") or 0 for u in usage)
    tout = sum(u.get("tokens_out") or 0 for u in usage)
    model = cfg.get("agent_models", {}).get(src.get("agent"))
    cost = tokens.cost(tin, tout, cfg.get("pricing", {}).get(model or "", {}))
    n_cov = len(covered)
    return {
        "source": spec, "gold": gold["label"], "checklist": ck.ref_of(doc),
        "coverage": {"gold_conversations": len(conv_ids), "predicted": n_cov,
                     "missing": [c for c in conv_ids if c not in results]},
        "macro_accuracy": macro, "macro_accuracy_ci95": macro_ci, "micro_accuracy": micro,
        "criteria": criteria_out,
        "no_na_confusion": {"gold_no_pred_na": sum(v["no_na"]["gold_no_pred_na"] for v in criteria_out.values()),
                            "gold_na_pred_no": sum(v["no_na"]["gold_na_pred_no"] for v in criteria_out.values())},
        "totals": {**totals, "mae_ci95": mae_ci},
        "evidence": {"items": ev_total, "idx_exists": stats.proportion(ev_exists, ev_total),
                     "quote_ok": stats.proportion(ev_quote, ev_total),
                     "match_gold_window": stats.proportion(ev_match, ev_considered),
                     "match_gold_exact": stats.proportion(ev_match_exact, ev_considered), "window": window},
        "validity": {"invalid_results": stats.proportion(invalid, n_cov), "missing_answers": missing_answers},
        "cost": {"calls": len(usage), "tokens_in": tin, "tokens_out": tout,
                 "tokens_per_dialog": round((tin + tout) / n_cov) if n_cov else None, "model": model,
                 "usd": cost, "usd_per_dialog": round(cost / n_cov, 6) if cost is not None and n_cov else None,
                 "token_method": "exact" if usage and all(u.get("exact") for u in usage) else "estimate"},
        "breakdown": breakdown,
        "errors": [{"conversation_id": d[0], "criterion_id": d[1], "gold": d[2], "pred": d[3]}
                   for d in decisions if not d[4]],
        "_decisions": decisions,
    }


def pairwise(results: list) -> list:
    """McNemar (ST.2) for every pair of sources on the decisions both made."""
    out = []
    for a, b in combinations(results, 2):
        da = {(d[0], d[1]): d[4] for d in a["_decisions"]}
        db = {(d[0], d[1]): d[4] for d in b["_decisions"]}
        common_keys = set(da) & set(db)
        only_a = sum(1 for k in common_keys if da[k] and not db[k])
        only_b = sum(1 for k in common_keys if db[k] and not da[k])
        out.append({"a": a["source"], "b": b["source"], "n": len(common_keys), "only_a_correct": only_a,
                    "only_b_correct": only_b, "p_value": stats.mcnemar(only_a, only_b)})
    return out


def compare(gold_spec: str, pred_specs: list, split: str = "all", actor: str = "system",
            save: bool = True) -> dict:
    """Head-to-head of several sources on the same labels (+ report files and a history event)."""
    gold = groundtruth.gold(gold_spec, split)
    doc = ck.load(gold["checklist"])
    cfg = common.load_config()
    results = [evaluate(gold, spec, doc, cfg) for spec in pred_specs]
    pairs = pairwise(results)
    report = {"gold": gold["label"], "split": split, "checklist": ck.ref_of(doc), "sources": results, "pairwise": pairs}
    clean = {**report, "sources": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]}
    if not save:
        return clean
    st_reports = list((common.REPORTS).glob("metrics-*.json")) if common.REPORTS.exists() else []
    report_id = f"m-{len(st_reports) + 1:04d}"
    save_json(common.REPORTS / f"metrics-{report_id}.json", clean)
    md = render(clean)
    (common.REPORTS / f"metrics-{report_id}.md").write_text(md, encoding="utf-8")
    events.append("metrics_computed", actor=actor, report_id=report_id, gold=gold["label"], split=split,
                  sources=pred_specs, path=common.rel(common.REPORTS / f"metrics-{report_id}.md"),
                  summary={r["source"]: {"macro": r["macro_accuracy"], "mae": r["totals"]["mae"],
                                         "n": r["coverage"]["predicted"]} for r in results})
    clean["report_id"] = report_id
    clean["paths"] = {"json": common.rel(common.REPORTS / f"metrics-{report_id}.json"),
                      "md": common.rel(common.REPORTS / f"metrics-{report_id}.md")}
    return clean


# --- «до и после» for a slide (sprint 1, item 5) ----------------------------------------------------

def before_after(gold_spec: str, before: str, after: str, split: str = "test", actor: str = "system",
                 save: bool = True) -> dict:
    """Two sources on the same held-out labels: accuracy before → after, McNemar, per-criterion change and the
    exact list of labels fixed / broken / still wrong."""
    gold = groundtruth.gold(gold_spec, split)
    doc = ck.load(gold["checklist"])
    cfg = common.load_config()
    rb, ra = evaluate(gold, before, doc, cfg), evaluate(gold, after, doc, cfg)
    db = {(d[0], d[1]): d for d in rb["_decisions"]}
    da = {(d[0], d[1]): d for d in ra["_decisions"]}
    both = sorted(set(db) & set(da))
    row = lambda k: {"conversation_id": k[0], "criterion_id": k[1], "gold": db[k][2],  # noqa: E731
                     "before": db[k][3], "after": da[k][3]}
    fixed = [row(k) for k in both if not db[k][4] and da[k][4]]
    broken = [row(k) for k in both if db[k][4] and not da[k][4]]
    still = [row(k) for k in both if not db[k][4] and not da[k][4]]
    changed_ok = [row(k) for k in both if db[k][4] and da[k][4] and db[k][3] != da[k][3]]
    per_crit = {}
    for cid, c in rb["criteria"].items():
        a = ra["criteria"].get(cid, {})
        per_crit[cid] = {"title": c["title"], "before": c["share"], "after": a.get("share"), "n": c["n"],
                         "fixed": sum(1 for f in fixed if f["criterion_id"] == cid),
                         "broken": sum(1 for f in broken if f["criterion_id"] == cid)}
    delta = lambda x, y: None if x is None or y is None else round(y - x, 4)  # noqa: E731
    rep = {
        "gold": gold["label"], "split": split, "checklist": ck.ref_of(doc), "before": before, "after": after,
        "decisions_compared": len(both), "conversations": len({k[0] for k in both}),
        "summary": {
            "macro": {"before": rb["macro_accuracy"], "after": ra["macro_accuracy"],
                      "delta_pp": None if delta(rb["macro_accuracy"], ra["macro_accuracy"]) is None
                      else round(delta(rb["macro_accuracy"], ra["macro_accuracy"]) * 100, 1),
                      "ci95_before": rb["macro_accuracy_ci95"], "ci95_after": ra["macro_accuracy_ci95"]},
            "micro": {"before": rb["micro_accuracy"]["share"], "after": ra["micro_accuracy"]["share"]},
            "mae_total": {"before": rb["totals"]["mae"], "after": ra["totals"]["mae"]},
            "within_tolerance": {"before": rb["totals"]["within_tolerance"]["share"],
                                 "after": ra["totals"]["within_tolerance"]["share"]},
            "no_na_confusion": {"before": rb["no_na_confusion"], "after": ra["no_na_confusion"]},
            "fixed": len(fixed), "broken": len(broken), "still_wrong": len(still),
            "mcnemar_p": stats.mcnemar(len(fixed), len(broken)),
            "cost_per_dialog": {"before": rb["cost"]["usd_per_dialog"], "after": ra["cost"]["usd_per_dialog"]},
        },
        "criteria": per_crit, "fixed": fixed, "broken": broken, "still_wrong": still,
        "changed_but_still_right": changed_ok,
        "coverage": {"before": rb["coverage"], "after": ra["coverage"]},
    }
    if not save:
        return rep
    existing = list(common.REPORTS.glob("before-after-*.json")) if common.REPORTS.exists() else []
    rid = f"ba-{len(existing) + 1:04d}"
    jp, mp = common.REPORTS / f"before-after-{rid}.json", common.REPORTS / f"before-after-{rid}.md"
    save_json(jp, rep)
    mp.write_text(render_before_after(rep), encoding="utf-8")
    events.append("before_after_computed", actor=actor, report_id=rid, gold=gold["label"], split=split,
                  before=before, after=after, path=common.rel(mp), summary=rep["summary"])
    rep["report_id"] = rid
    rep["paths"] = {"json": common.rel(jp), "md": common.rel(mp)}
    return rep


def render_before_after(rep: dict) -> str:
    s = rep["summary"]
    m = s["macro"]
    sign = lambda x: "—" if x is None else f"{x:+.1f} п. п."  # noqa: E731
    L = [f"# До и после: {rep['before']} → {rep['after']}", "",
         f"- Эталон `{rep['gold']}`, часть `{rep['split']}` (не использовалась для настройки) · "
         f"{rep['conversations']} диалогов, {rep['decisions_compared']} решений",
         f"- Макро-точность: **{_pct(m['before'])} → {_pct(m['after'])}** ({sign(m['delta_pp'])})",
         f"- Исправлено **{s['fixed']}**, сломано **{s['broken']}**, осталось ошибок {s['still_wrong']} · "
         f"Макнемар p = {s['mcnemar_p'] if s['mcnemar_p'] is not None else '—'}",
         f"- MAE итога: {s['mae_total']['before']} → {s['mae_total']['after']} · "
         f"«Нет»↔N/A: {sum(s['no_na_confusion']['before'].values())} → {sum(s['no_na_confusion']['after'].values())}",
         "", "## По критериям", "", "| Критерий | До | После | Исправлено | Сломано |", "|---|---|---|---|---|"]
    for cid, c in rep["criteria"].items():
        L.append(f"| {cid} {c['title']} | {_pct(c['before'])} | {_pct(c['after'])} | {c['fixed']} | {c['broken']} |")
    for title, key in (("Исправлено", "fixed"), ("Сломано", "broken"), ("Осталось ошибок", "still_wrong")):
        rows = rep[key]
        L += ["", f"## {title} ({len(rows)})", ""]
        if rows:
            L += ["| Диалог | Критерий | Эталон | До | После |", "|---|---|---|---|---|"]
            L += [f"| {r['conversation_id']} | {r['criterion_id']} | {r['gold']} | {r['before']} | {r['after']} |"
                  for r in rows]
        else:
            L.append("—")
    L += ["", "На 26 диалогах одно решение — около 0,5 п. п.; смотрите число исправленных и сломанных и p, "
              "а не только разницу процентов."]
    return "\n".join(L) + "\n"


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def render(rep: dict) -> str:
    srcs = rep["sources"]
    L = [f"# Точность относительно эталона {rep['gold']}", "",
         f"- Чек-лист эталона: `{rep['checklist']}` · часть: `{rep['split']}`",
         "- Итоги пересчитаны формулой платформы из ответов; метрики — только по общим критериям.", "",
         "## Сводка", "",
         "| Источник | Диалогов | Макро-точность (95% ДИ) | Микро | «Нет»↔N/A | MAE итога | ±{:g} баллов | Доказательства ±{} | Цитаты ок | Невалидных | Токены / диалог | $ / диалог |".format(
             srcs[0]["totals"]["tolerance"] if srcs else 5, srcs[0]["evidence"]["window"] if srcs else 1),
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in srcs:
        ci = r["macro_accuracy_ci95"]
        nn = r["no_na_confusion"]
        L.append(f"| {r['source']} | {r['coverage']['predicted']}/{r['coverage']['gold_conversations']} | "
                 f"{_pct(r['macro_accuracy'])} ({_pct(ci[0]) if ci else '—'}–{_pct(ci[1]) if ci else '—'}) | "
                 f"{_pct(r['micro_accuracy']['share'])} | {nn['gold_no_pred_na']}+{nn['gold_na_pred_no']} | "
                 f"{r['totals']['mae'] if r['totals']['mae'] is not None else '—'} | "
                 f"{_pct(r['totals']['within_tolerance']['share'])} | {_pct(r['evidence']['match_gold_window']['share'])} | "
                 f"{_pct(r['evidence']['quote_ok']['share'])} | {_pct(r['validity']['invalid_results']['share'])} | "
                 f"{r['cost']['tokens_per_dialog'] or '—'} | {r['cost']['usd_per_dialog'] if r['cost']['usd_per_dialog'] is not None else '—'} |")
    if srcs:
        L += ["", "## Точность по критериям (n, κ)", "",
              "| Критерий | " + " | ".join(r["source"] for r in srcs) + " |", "|---|" + "---|" * len(srcs)]
        for cid, c in srcs[0]["criteria"].items():
            cells = []
            for r in srcs:
                x = r["criteria"][cid]
                cells.append(f"{_pct(x['share'])} (n={x['n']}, κ={x['kappa'] if x['kappa'] is not None else '—'})")
            L.append(f"| {cid} {c['title']} | " + " | ".join(cells) + " |")
        L += ["", "## Разбивка по языку и каналу (микро-точность)", "",
              "| Срез | " + " | ".join(r["source"] for r in srcs) + " |", "|---|" + "---|" * len(srcs)]
        keys = sorted(set().union(*[set(r["breakdown"]) for r in srcs]))
        for k in keys:
            L.append(f"| {k} | " + " | ".join(
                f"{_pct(r['breakdown'][k]['share'])} (n={r['breakdown'][k]['n']})" if k in r["breakdown"] else "—"
                for r in srcs) + " |")
    if rep["pairwise"]:
        L += ["", "## Парное сравнение (тест Макнемара)", "",
              "| A | B | Общих решений | Верно только A | Верно только B | p |", "|---|---|---|---|---|---|"]
        for p in rep["pairwise"]:
            L.append(f"| {p['a']} | {p['b']} | {p['n']} | {p['only_a_correct']} | {p['only_b_correct']} | "
                     f"{p['p_value'] if p['p_value'] is not None else '—'} |")
    for r in srcs:
        if r["errors"]:
            L += ["", f"## Ошибки: {r['source']} ({len(r['errors'])})", "", "| Диалог | Критерий | Эталон | Ответ |",
                  "|---|---|---|---|"]
            L += [f"| {e['conversation_id']} | {e['criterion_id']} | {e['gold']} | {e['pred']} |" for e in r["errors"]]
    L += ["", "Различие в несколько процентов на ~20 диалогах — на грани шума: смотрите ДИ и p-value, а не одну цифру."]
    return "\n".join(L) + "\n"


# --- agreement between sources without gold: stability (M.10), annotator agreement (G.5),
#     interference after a checklist change (M.15) ---------------------------------------------------

def agreement(pred_specs: list, criteria: Optional[list] = None) -> dict:
    loaded = [sources.load_results(s) for s in pred_specs]
    convs = sorted(set.intersection(*[set(r) for _, r in loaded])) if loaded else []
    crit_ids = criteria or sorted(set().union(*[set((r[c].get("answers") or {})) for _, r in loaded for c in convs]))
    per = {}
    for cid in crit_ids:
        rows = [[key((res[c].get("answers") or {}).get(cid)) for _, res in loaded] for c in convs]
        rows = [r for r in rows if all(v is not None for v in r)]
        all_agree = sum(len(set(r)) == 1 for r in rows)
        pair = {}
        for i, j in combinations(range(len(loaded)), 2):
            pairs = [(r[i], r[j]) for r in rows]
            pair[f"{pred_specs[i]}↔{pred_specs[j]}"] = {"agree": stats.proportion(sum(a == b for a, b in pairs),
                                                                                 len(pairs)),
                                                          "kappa": stats.kappa(pairs)}
        per[cid] = {"all_agree": stats.proportion(all_agree, len(rows)), "pairs": pair}
    changed = [{"conversation_id": c, "criterion_id": cid,
                "answers": [(res[c].get("answers") or {}).get(cid) for _, res in loaded]}
               for c in convs for cid in crit_ids
               if len({key((res[c].get("answers") or {}).get(cid)) for _, res in loaded}) > 1]
    total = sum(v["all_agree"]["n"] for v in per.values())
    agree = sum(v["all_agree"]["k"] for v in per.values())
    return {"sources": pred_specs, "conversations": len(convs), "criteria": per,
            "overall": stats.proportion(agree, total), "differences": changed}
