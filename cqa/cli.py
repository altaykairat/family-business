"""Command line entry point: python3 -m cqa <command> ..."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import checklist as ck
from . import report, run
from .common import load_config, rel


def cmd_checklist(args):
    if args.action == "list":
        for cid in sorted(p.name for p in ck.CHECKLISTS.iterdir() if p.is_dir()):
            for v in ck.versions(cid):
                d = ck.load(f"{cid}@{v}")
                m = d["meta"]
                print(f"{cid}@v{v}  {m.get('created_at', '')}  {m.get('author', '')}  "
                      f"критериев: {len(ck.active_criteria(d))}  hash: {m['content_hash']}  — {m.get('reason', '')}")
    elif args.action == "show":
        d = ck.load(args.ref)
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif args.action == "diff":
        for line in ck.diff(ck.load(args.ref), ck.load(args.other)):
            print(line)
    elif args.action == "save":
        out = ck.save_new_version(Path(args.file), args.id, args.author, args.reason)
        print(f"Сохранена новая версия: {rel(out)}")


def cmd_prepare(args):
    cfg = load_config()
    if args.agents:
        cfg["agents"] = args.agents
    if args.no_facts:
        cfg["include_facts"] = False
    if args.template:
        cfg["template"] = args.template
    run_dir = run.prepare(args.run, args.dataset, args.checklist, args.variant, cfg,
                          ids=args.ids, sample=args.sample, seed=args.seed)
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    parts = sum(len(c["parts"]) for c in m["conversations"].values())
    print(f"Прогон {args.run}: {len(m['conversations'])} диалогов, {parts} промптов на агента, "
          f"агенты: {', '.join(m['agents'])}")
    print(f"Промпты для вставки: {rel(run_dir)}/agents/<агент>/prompts/  ·  ответы: .../responses/")
    for v, p in m["token_projection"].items():
        print(f"  проекция {v}: {p['calls']} вызовов, ~{p['tokens_in']} входных токенов ({m['token_method']})")


def cmd_ingest(args):
    summary = run.ingest(args.run, load_config(), args.agents)
    for agent, st in summary.items():
        print(f"{agent}: получено {st['done']} (частично {st['partial']}), ждут {st['pending']} · "
              f"ok {st['ok']}, предупреждения {st['warn']}, ошибки {st['error']}")


def cmd_report(args):
    out = report.build(args.run, load_config())
    print(f"Отчёт: {out['summary']}  ·  расхождений: {out['disagreements']}  ·  ответов: {out['answered']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="cqa", description="CQA pipeline (manual mode, iteration 1)")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("checklist", help="версии чек-листа")
    c.add_argument("action", choices=["list", "show", "diff", "save"])
    c.add_argument("ref", nargs="?", default="default", help="default | default@1 | путь к JSON")
    c.add_argument("other", nargs="?", help="вторая версия для diff")
    c.add_argument("--file", help="для save: отредактированный JSON")
    c.add_argument("--id", default="default", help="для save: id чек-листа")
    c.add_argument("--author", default="admin")
    c.add_argument("--reason", default="")
    c.set_defaults(func=cmd_checklist)

    pp = sub.add_parser("prepare", help="S1–S3: подготовить прогон и промпты")
    pp.add_argument("--run", required=True, help="id прогона, напр. r001")
    pp.add_argument("--dataset", default="dev")
    pp.add_argument("--checklist", default="default", help="default | default@1")
    pp.add_argument("--variant", default="v1", choices=["v1", "v2", "v3"])
    pp.add_argument("--template", help="имя шаблона из prompts/templates (по умолчанию из config)")
    pp.add_argument("--ids", nargs="*", help="конкретные диалоги")
    pp.add_argument("--sample", type=int, default=0, help="стратифицированная выборка из N диалогов")
    pp.add_argument("--seed", type=int, default=42)
    pp.add_argument("--agents", nargs="*", help="переопределить список агентов")
    pp.add_argument("--no-facts", action="store_true", help="не передавать вычисленные факты в промпт")
    pp.set_defaults(func=cmd_prepare)

    i = sub.add_parser("ingest", help="S5–S7: прочитать ответы, проверить, посчитать баллы")
    i.add_argument("--run", required=True)
    i.add_argument("--agents", nargs="*")
    i.set_defaults(func=cmd_ingest)

    r = sub.add_parser("report", help="S6/S8/S9: сравнение агентов, большинство, токены")
    r.add_argument("--run", required=True)
    r.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
