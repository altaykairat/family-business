"""CLI interface for Antigravity Fine-Grained Decomposition Prototypes.

Usage:
  python3 -m variants.antigravity prepare --run r_anti_001 --variant v3_focus --preset dev5
  python3 -m variants.antigravity simulate --run r_anti_001
  python3 -m variants.antigravity ingest --run r_anti_001
  python3 -m variants.antigravity report --run r_anti_001
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cqa.common import load_json

from .runner import ALL_ANTIGRAVITY_VARIANTS, ingest, prepare, report, simulate


def main(argv: list = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m variants.antigravity",
        description="CQA Antigravity Alternative Pipeline Prototypes: Fine-Grained Decomposition",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # prepare
    p_prep = sub.add_parser("prepare", help="Generate prompts and manifest for a run")
    p_prep.add_argument("--run", required=True, help="Run identifier (e.g. r_anti_001)")
    p_prep.add_argument("--dataset", default="DEV", help="Dataset name in data/ (default DEV)")
    p_prep.add_argument("--checklist", default="default@1", help="Checklist ref (default default@1)")
    p_prep.add_argument("--variant", default="v3_focus", choices=ALL_ANTIGRAVITY_VARIANTS,
                        help="Decomposition variant (default v3_focus)")
    p_prep.add_argument("--preset", choices=["dev5", "dev15", "test", "tune"], default=None,
                        help="Pre-configured curated subset (dev5, dev15, test, tune)")
    p_prep.add_argument("--split", choices=["test", "tune"], default=None,
                        help="Split from dev-gold (test=26 dialogs, tune=39 dialogs)")
    p_prep.add_argument("--sample", type=int, default=0, help="Random stratified sample size")
    p_prep.add_argument("--ids", help="Comma-separated conversation IDs")
    p_prep.add_argument("--no-facts", action="store_true", help="Omit S2 features in prompts")

    # simulate
    p_sim = sub.add_parser("simulate", help="Generate synthetic mock responses for dry-runs")
    p_sim.add_argument("--run", required=True, help="Run identifier")
    p_sim.add_argument("--agent", default="antigravity", help="Agent name")

    # ingest
    p_ing = sub.add_parser("ingest", help="Ingest responses, validate, reduce sub-questions and score")
    p_ing.add_argument("--run", required=True, help="Run identifier")
    p_ing.add_argument("--agents", help="Comma-separated agent names")

    # report
    p_rep = sub.add_parser("report", help="Build comparison and token economics report")
    p_rep.add_argument("--run", required=True, help="Run identifier")

    args = parser.parse_args(argv)

    if args.cmd == "prepare":
        cfg = {"include_facts": not args.no_facts, "agents": ["antigravity", "claude", "codex"]}
        cid_list = [i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else None
        out_dir = prepare(
            run_id=args.run,
            dataset=args.dataset,
            checklist_ref=args.checklist,
            variant=args.variant,
            cfg=cfg,
            ids=cid_list,
            preset=args.preset,
            split=args.split,
            sample=args.sample,
        )
        print(f"Готово: прогон подготовлен в {out_dir}")
        print(f"Промпты: {out_dir}/prompts/ (всего файлов готово к оценке)")

    elif args.cmd == "simulate":
        count = simulate(args.run, args.agent)
        print(f"Сгенерировано {count} мок-ответов для агента {args.agent} в {args.run}")

    elif args.cmd == "ingest":
        ag_list = [a.strip() for a in args.agents.split(",") if a.strip()] if args.agents else None
        res = ingest(args.run, agents=ag_list)
        print(f"Ingest завершён: {res}")

    elif args.cmd == "report":
        res = report(args.run)
        print(f"Отчёт сформирован: {res['report_file']}")


if __name__ == "__main__":
    main()
