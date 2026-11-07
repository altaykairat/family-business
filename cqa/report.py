"""S6 aggregation + S8/S9 reports: head-to-head comparison of agents, majority vote, tokens and cost."""

from __future__ import annotations

import csv
from collections import Counter
from itertools import combinations
from statistics import median

from . import checklist as ck
from . import scoring, tokens
from .common import RUNS, load_json, log_event, rel


def _load(run_dir, agents):
    out = {}
    for agent in agents:
        folder = run_dir / "validated" / agent
        out[agent] = {p.stem: load_json(p) for p in sorted(folder.glob("*.json"))} if folder.exists() else {}
    return out


def _spread(totals: dict):
    vals = [t for t in totals.values() if t is not None]
    return round(max(vals) - min(vals), 2) if len(vals) >= 2 else None


def majority(values: list, c: dict):
    """Majority across agents; for scales falls back to the median step when there is no majority."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, "нет ответов"
    top, n = Counter(vals).most_common(1)[0]
    if n > len(vals) / 2:
        return top, f"{n}/{len(vals)}"
    nums = [v for v in vals if isinstance(v, (int, float))]
    if c["type"] == "scale" and len(nums) == len(vals):
        steps = sorted(s["value"] for s in c["scale"])
        m = median(nums)
        return min(steps, key=lambda s: (abs(s - m), s)), f"медиана из {len(vals)}"
    return None, f"нет большинства ({dict(Counter(vals))})"


def build(run_id: str, cfg: dict) -> dict:
    run_dir = RUNS / run_id
    manifest = load_json(run_dir / "manifest.json")
    doc = ck.load(manifest["checklist"].replace("@v", "@"))
    crit = ck.active_criteria(doc)
    agents = manifest["agents"]
    res = _load(run_dir, agents)
    convs = list(manifest["conversations"])
    rep = run_dir / "reports"
    rep.mkdir(parents=True, exist_ok=True)

    # --- long table: one row per agent × conversation × criterion (history-friendly) -------
    with open(rep / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "checklist", "variant", "agent", "conversation_id", "criterion_id", "answer",
                    "points", "effective_weight", "effective_points", "confidence", "evidence_idx",
                    "total", "gross_error", "status", "explanation"])
        for agent in agents:
            for cid, r in res[agent].items():
                for row in r["score"]["rows"]:
                    item = r["criteria"].get(row["criterion_id"], {})
                    w.writerow([run_id, manifest["checklist"], manifest["variant"], agent, cid, row["criterion_id"],
                                row["answer"], row["points"], row["effective_weight"], row["effective_points"],
                                item.get("confidence"), " ".join(str(e.get("idx")) for e in item.get("evidence") or []),
                                r["score"]["total"], r["score"]["gross_error"], r["status"], item.get("explanation")])

    # --- majority vote + per-conversation comparison -----------------------------------------
    comparison, disagreements, majority_rows = [], [], []
    full_agree = Counter()
    pair_agree = {p: Counter() for p in combinations(agents, 2)}
    pair_total = {p: Counter() for p in combinations(agents, 2)}
    for cid in convs:
        answered = [a for a in agents if cid in res[a]]
        totals = {a: res[a][cid]["score"]["total"] for a in answered}
        maj_answers = {}
        for c in crit:
            vals = {a: res[a][cid]["answers"].get(c["id"]) for a in answered}
            m, note = majority(list(vals.values()), c)
            if m is not None:
                maj_answers[c["id"]] = m
            present = [v for v in vals.values() if v is not None]
            if len(present) >= 2 and len(set(map(str, present))) == 1:
                full_agree[c["id"]] += 1
            elif len(set(map(str, present))) > 1:
                disagreements.append({"conversation_id": cid, "criterion_id": c["id"], "answers": vals, "majority": m, "note": note})
            for p in pair_agree:
                if vals.get(p[0]) is not None and vals.get(p[1]) is not None:
                    pair_total[p][c["id"]] += 1
                    pair_agree[p][c["id"]] += int(str(vals[p[0]]) == str(vals[p[1]]))
        maj_score = scoring.score(doc, maj_answers) if answered else None
        model_est = {a: (res[a][cid].get("model_total_estimate") or {}).get("total") for a in answered}
        comparison.append({"conversation_id": cid, "totals": totals, "majority_total": maj_score and maj_score["total"],
                           "spread": _spread(totals),
                           "gross": {a: res[a][cid]["score"]["gross_error"] for a in answered}, "model_estimates": model_est})
        if maj_score:
            majority_rows.append({"conversation_id": cid, **{c["id"]: maj_answers.get(c["id"]) for c in crit},
                                  "total": maj_score["total"], "gross_error": maj_score["gross_error"]})

    with open(rep / "majority.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["conversation_id"] + [c["id"] for c in crit] + ["total", "gross_error"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(majority_rows)

    # --- tokens and cost ------------------------------------------------------------------
    pricing = cfg.get("pricing", {})
    agent_models = cfg.get("agent_models", {})
    cost_rows = []
    for agent in agents:
        tin = sum(u["tokens_in"] for r in res[agent].values() for u in r["usage"])
        tout = sum(u["tokens_out"] for r in res[agent].values() for u in r["usage"])
        exact = sum(1 for r in res[agent].values() for u in r["usage"] if u["exact"])
        calls = sum(len(r["usage"]) for r in res[agent].values())
        n = len(res[agent])
        price = pricing.get(agent_models.get(agent, ""), {})
        cost = tokens.cost(tin, tout, price)
        cost_rows.append({"agent": agent, "model": agent_models.get(agent), "dialogs": n, "calls": calls,
                          "tokens_in": tin, "tokens_out": tout, "exact_usage_calls": exact,
                          "avg_in_per_dialog": round(tin / n) if n else None,
                          "avg_out_per_dialog": round(tout / n) if n else None,
                          "cost_usd": cost, "cost_per_dialog_usd": round(cost / n, 6) if cost is not None and n else None})

    # --- markdown summary ------------------------------------------------------------------
    L = [f"# Отчёт прогона {run_id}", "",
         f"- Чек-лист: `{manifest['checklist']}` · шаблон: `{manifest['template']}` · вариант: `{manifest['variant']}` · "
         f"параметры: `{manifest['params_hash']}`",
         f"- Диалогов в прогоне: {len(convs)} · агенты: {', '.join(agents)}",
         f"- Метод подсчёта токенов: `{manifest['token_method']}` (точные значения — только где агент показал расход)", ""]

    L += ["## Статус ответов", "", "| Агент | Получено | OK | Предупреждения | Ошибки |", "|---|---|---|---|---|"]
    for agent in agents:
        st = Counter(r["status"] for r in res[agent].values())
        L.append(f"| {agent} | {len(res[agent])}/{len(convs)} | {st['ok']} | {st['warn']} | {st['error']} |")

    L += ["", "## Согласие агентов по критериям", "",
          "Доля диалогов, где все ответившие агенты дали одинаковый ответ; ниже — попарное согласие.", "",
          "| Критерий | Все согласны | " + " | ".join(f"{a}↔{b}" for a, b in pair_agree) + " |",
          "|---|---|" + "---|" * len(pair_agree)]
    n_conv = max(1, len([c for c in convs if sum(c in res[a] for a in agents) >= 2]))
    for c in crit:
        pairs = " | ".join(f"{pair_agree[p][c['id']]}/{pair_total[p][c['id']]}" for p in pair_agree)
        L.append(f"| {c['id']} {c['title']} | {full_agree[c['id']]}/{n_conv} | {pairs} |")

    L += ["", "## Итоговые баллы по диалогам", "",
          "| Диалог | " + " | ".join(agents) + " | Большинство | Разброс | Грубая ошибка | Оценка итога моделью |",
          "|---|" + "---|" * (len(agents) + 4)]
    for row in comparison:
        cells = " | ".join(str(row["totals"].get(a, "—")) for a in agents)
        gross = ", ".join(a for a, g in row["gross"].items() if g) or "—"
        est = ", ".join(f"{a}: {v}" for a, v in row["model_estimates"].items() if v is not None) or "—"
        L.append(f"| {row['conversation_id']} | {cells} | {row['majority_total']} | {row['spread']} | {gross} | {est} |")

    L += ["", f"## Расхождения ({len(disagreements)})", "", "| Диалог | Критерий | Ответы | Большинство |", "|---|---|---|---|"]
    for d in disagreements:
        ans = ", ".join(f"{a}: {v}" for a, v in d["answers"].items())
        L.append(f"| {d['conversation_id']} | {d['criterion_id']} | {ans} | {d['majority'] if d['majority'] is not None else d['note']} |")

    L += ["", "## Токены и стоимость", "",
          "| Агент | Модель | Диалогов | Вызовов | Токены вход | Токены выход | Точных замеров | Вход / диалог | Выход / диалог | $ всего | $ / диалог |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in cost_rows:
        L.append("| " + " | ".join(str(r[k]) if r[k] is not None else "—" for k in
                 ("agent", "model", "dialogs", "calls", "tokens_in", "tokens_out", "exact_usage_calls",
                  "avg_in_per_dialog", "avg_out_per_dialog", "cost_usd", "cost_per_dialog_usd")) + " |")
    ens_in = sum(r["tokens_in"] for r in cost_rows)
    ens_out = sum(r["tokens_out"] for r in cost_rows)
    L.append(f"| **ансамбль (все агенты)** | | | | {ens_in} | {ens_out} | | | | | |")

    proj = manifest["token_projection"]
    L += ["", "### Проекция входных токенов по вариантам декомпозиции (на этом наборе диалогов)", "",
          "| Вариант | Вызовов | Входных токенов | Вход / диалог | × к v1 |", "|---|---|---|---|---|"]
    base = proj["v1"]["tokens_in"] or 1
    for v, p in proj.items():
        L.append(f"| {v} | {p['calls']} | {p['tokens_in']} | {round(p['tokens_in'] / max(1, len(convs)))} | "
                 f"{p['tokens_in'] / base:.2f} |")
    L += ["", "Выходные токены для вариантов, которые ещё не прогоняли, будут измерены после прогона.",
          "Цены моделей задаются в `config/pricing.json`, соответствие агент → модель — в `config/pipeline.json` (`agent_models`)."]

    (rep / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log_event("report_built", run_id=run_id, disagreements=len(disagreements),
              answered={a: len(res[a]) for a in agents})
    return {"summary": rel(rep / "summary.md"), "disagreements": len(disagreements),
            "answered": {a: len(res[a]) for a in agents}}
