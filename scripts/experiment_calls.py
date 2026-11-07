"""Experiment «сколько LLM-вызовов на подзадачу»: decomposition (V1 / V2 / V3) × model, cost in API-equivalent $.

Input: runs prepared by `cqa run.prepare` whose responses were produced by isolated subagents (one subagent =
one API call, see docs/team/implementation.md → «Субагенты вместо API»). Optional --box copies responses from an
isolated folder (outside the repo) into runs/<run>/agents/<agent>/responses first.

Accuracy: without human ground truth the script reports a PROXY — agreement with the leave-one-out consensus of
all other configurations (the configuration itself never votes for its own reference). With --gold set@vN it also
reports real accuracy through cqa.metrics.

Usage:
  python3 scripts/experiment_calls.py --runs sa_v1 sa_v2 sa_v3 [--box /path/llm-box] [--gold dev-gold@v1]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cqa import checklist as ck  # noqa: E402
from cqa import common, groundtruth, metrics, run, scoring, sources, stats, tokens  # noqa: E402


def copy_back(box: Path, runs: list) -> int:
    n = 0
    for r in runs:
        for f in (box / r).glob("*/responses/*.json"):
            dst = common.RUNS / r / "agents" / f.parent.parent.name / "responses" / f.name
            if f.read_text(encoding="utf-8").strip():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, dst)
                n += 1
    return n


def configs(runs: list) -> list:
    out = []
    for r in runs:
        m = common.load_json(common.RUNS / r / "manifest.json")
        for a in m["agents"]:
            out.append({"spec": f"{r}:{a}", "run": r, "agent": a, "variant": m["variant"],
                        "calls_per_dialog": len(next(iter(m["conversations"].values()))["parts"])})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--box")
    ap.add_argument("--gold")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.box:
        print("copied responses:", copy_back(Path(a.box), a.runs))
    cfg = common.load_config()
    for r in a.runs:
        print(r, run.ingest(r, cfg))
    confs = configs(a.runs)
    doc = ck.load("default@1")
    crit = [c["id"] for c in ck.active_criteria(doc)]
    loaded = {c["spec"]: sources.load_results(c["spec"])[1] for c in confs}
    convs = sorted(set.intersection(*[set(v) for v in loaded.values()]))

    rows = []
    for c in confs:
        others = [s for s in loaded if s != c["spec"]]
        gold_items = {}
        for conv in convs:
            labels = {}
            for cid in crit:
                votes = Counter(metrics.key((loaded[s][conv].get("answers") or {}).get(cid)) for s in others)
                votes.pop(None, None)
                if votes:
                    top, n = votes.most_common(1)[0]
                    if n > sum(votes.values()) / 2:
                        labels[cid] = {"answer": top if not top.lstrip("-").isdigit() else int(top), "evidence_idx": []}
            gold_items[conv] = labels
        gold = {"label": "leave-one-out consensus", "items": gold_items, "split": None}
        ev = metrics.evaluate(gold, c["spec"], doc, cfg)
        res = loaded[c["spec"]]
        usage = [u for conv in convs for u in res[conv].get("usage") or []]
        tin = sum(u["tokens_in"] for u in usage)
        tout = sum(u["tokens_out"] for u in usage)
        model = cfg["agent_models"].get(c["agent"])
        price = cfg["pricing"].get(model, {})
        usd = tokens.cost(tin, tout, price)
        invalid = sum(1 for conv in convs if res[conv].get("status") == "error")
        missing = sum(len(set(crit) - set(res[conv].get("answers") or {})) for conv in convs)
        quote = ev["evidence"]["quote_ok"]["share"]
        rows.append({**c, "model": model, "dialogs": len(convs), "tokens_in": tin, "tokens_out": tout,
                     "usd": usd, "usd_per_dialog": usd / len(convs) if usd is not None else None,
                     "proxy_macro": ev["macro_accuracy"], "proxy_micro": ev["micro_accuracy"]["share"],
                     "proxy_n": ev["micro_accuracy"]["n"], "proxy_ci": ev["macro_accuracy_ci95"],
                     "quote_ok": quote, "invalid": invalid, "missing_answers": missing,
                     "per_criterion": {k: v["share"] for k, v in ev["criteria"].items()},
                     "errors": ev["errors"]})
    real = None
    if a.gold:
        real = metrics.compare(a.gold, [c["spec"] for c in confs], save=True)

    agr = metrics.agreement([c["spec"] for c in confs if c["run"] == a.runs[0] and c["agent"] in ("sonnet", "sonnet2")]) \
        if any(c["agent"] == "sonnet2" for c in confs) else None

    L = ["# Эксперимент: число LLM-вызовов на подзадачу × модель", "",
         f"- Диалогов: {len(convs)} ({', '.join(convs)}) · чек-лист `default@v1` · шаблон `v1`, одинаковый для всех вариантов",
         "- Исполнитель: изолированный субагент Claude Code на каждый промпт (один субагент = один API-вызов)",
         f"- Токены: `{tokens.METHOD}` (оценка по тексту промпта и ответа; токены размышлений не видны — реальная стоимость выше)",
         "- Точность: **прокси** — совпадение с консенсусом остальных конфигураций (leave-one-out). Это мера согласия, "
         "не правильности: настоящая точность — только против эталона людей.", "",
         "| Конфигурация | Вызовов / диалог | Модель | Токены вход / диалог | Токены выход / диалог | $ / диалог (API) | $ / 65 диалогов | $ / 10 контрольных | Прокси-макро (95% ДИ) | Прокси-микро | Цитаты ок | Невалидных | Пропусков |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = r["dialogs"]
        ci = r["proxy_ci"]
        L.append(f"| {r['spec']} | {r['calls_per_dialog']} | {r['model']} | {r['tokens_in'] // d} | {r['tokens_out'] // d} | "
                 f"{r['usd_per_dialog']:.4f} | {r['usd_per_dialog'] * 65:.2f} | {r['usd_per_dialog'] * 10:.3f} | "
                 f"{(r['proxy_macro'] or 0) * 100:.1f}% ({ci[0] * 100:.0f}–{ci[1] * 100:.0f}) | {(r['proxy_micro'] or 0) * 100:.1f}% | "
                 f"{(r['quote_ok'] or 0) * 100:.0f}% | {r['invalid']} | {r['missing_answers']} |")
    L += ["", "## Прокси-точность по критериям", "", "| Конфигурация | " + " | ".join(crit) + " |",
          "|---|" + "---|" * len(crit)]
    for r in rows:
        L.append(f"| {r['spec']} | " + " | ".join("—" if r["per_criterion"][c] is None else f"{r['per_criterion'][c] * 100:.0f}%"
                                                  for c in crit) + " |")
    if agr:
        L += ["", f"## Стабильность (M.10): sonnet V1, два независимых прогона — совпадение {agr['overall']['share'] * 100:.1f}% "
                  f"(n={agr['overall']['n']})", ""]
        for d in agr["differences"]:
            L.append(f"- {d['conversation_id']} / {d['criterion_id']}: {d['answers']}")
    L += ["", "## Расхождения с консенсусом (ответ конфигурации ≠ консенсус остальных)", ""]
    for r in rows:
        if r["errors"]:
            L.append(f"- **{r['spec']}**: " + ", ".join(f"{e['conversation_id']}/{e['criterion_id']} {e['pred']}≠{e['gold']}"
                                                         for e in r["errors"]))
    if real:
        L += ["", f"## Настоящая точность против {real['gold']}", "", f"Отчёт: `{real['paths']['md']}`"]
    out = Path(a.out) if a.out else common.RUNS / a.runs[0] / "reports" / "experiment_calls.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    common.save_json(out.with_suffix(".json"), rows)
    print("report:", common.rel(out))


if __name__ == "__main__":
    main()
