"""Runner and Orchestration Engine for Antigravity Fine-Grained Decomposition Variants.

Manages:
  prepare  — S1–S3 generation of prompts, manifest, and token projections
  simulate — Generate synthetic valid mock responses for dry-runs and automated tests
  ingest   — S5–S7 parsing, validation, deterministic subquestion reduction, scoring
  report   — S8–S9 comparison, tokens, cost economics in USD, and CSV exports
"""

from __future__ import annotations

import csv
import json
import random
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cqa.checklist as ck
import cqa.conversation as cv
import cqa.scoring as scoring
import cqa.tokens as tokens
from cqa.common import RUNS, load_json, log_event, now_iso, rel, save_json, stable_hash
from cqa.responses import normalize_answer, parse as base_parse, quote_coverage

from .dossier import render_dossier_markdown, validate_dossier
from .prompts import build_prompts
from .subquestions import derive_subquestions, reduce_subquestions

ALL_ANTIGRAVITY_VARIANTS = (
    "v1",
    "v3_focus",
    "v4_facts",
    "v4_mono",
    "v4_focus",
    "v_subq",
    "v_subq_bundled",
)

# High-signal curated subsets for rapid development and testing
PRESETS = {
    "dev5": [
        "p1-ru-001",  # Standard greeting, tariff info, clean dialogue
        "p1-ru-012",  # Edge case: connection dropped abruptly (c07 N/A vs No)
        "p1-ru-022",  # Trap case: operator solicits personal transfer (c08 gross error)
        "p1-kz-008",  # Kazakh language chat, tests language matching (c01)
        "p4-ru-001",  # Real phone call with ASR noise and timestamps
    ],
    "dev15": [
        "p1-ru-001", "p1-ru-005", "p1-ru-012", "p1-ru-014", "p1-ru-022",
        "p1-ru-029", "p1-kz-001", "p1-kz-005", "p1-kz-008", "p1-kz-010",
        "p1-en-001", "p1-en-005", "p1-en-012", "p4-ru-001", "p4-ru-003",
    ],
}

MODEL_PRICES = {
    "gemini_flash": {"name": "Gemini 1.5 Flash", "input_per_mtok": 0.075, "output_per_mtok": 0.30},
    "claude_sonnet": {"name": "Claude 3.5 Sonnet", "input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "gpt4o": {"name": "GPT-4o", "input_per_mtok": 2.50, "output_per_mtok": 10.00},
    "gpt4o_mini": {"name": "GPT-4o mini", "input_per_mtok": 0.15, "output_per_mtok": 0.60},
}


def _select_ids(
    dataset: str,
    ids: Optional[List[str]],
    preset: Optional[str],
    sample: int,
    seed: int = 42,
    split: Optional[str] = None,
) -> List[str]:
    all_ids = cv.list_ids(dataset)
    if ids:
        missing = sorted(set(ids) - set(all_ids))
        if missing:
            raise SystemExit(f"Диалоги не найдены в наборе {dataset}: {missing}")
        return ids
    if split or preset in ("test", "tune"):
        target = split or preset
        try:
            from cqa import groundtruth
            gt_data = groundtruth.load("dev-gold")
            split_ids = gt_data.get("split", {}).get(target, [])
            if split_ids:
                return sorted(split_ids)
        except Exception:
            pass
    if preset:
        if preset in PRESETS:
            return PRESETS[preset]
        raise SystemExit(f"Неизвестный пресет {preset!r}. Доступны: {list(PRESETS.keys()) + ['test', 'tune']}")
    if sample and sample < len(all_ids):
        rng = random.Random(seed)
        by_group: Dict[str, List[str]] = {}
        for cid in all_ids:
            by_group.setdefault(cid.rsplit("-", 1)[0], []).append(cid)
        picked = []
        for g in sorted(by_group):
            k = max(1, round(sample * len(by_group[g]) / len(all_ids)))
            picked += rng.sample(by_group[g], min(k, len(by_group[g])))
        return sorted(picked)[:sample]
    return all_ids


def prepare(
    run_id: str,
    dataset: str = "DEV",
    checklist_ref: str = "default@1",
    variant: str = "v3_focus",
    cfg: Optional[dict] = None,
    ids: Optional[List[str]] = None,
    preset: Optional[str] = None,
    split: Optional[str] = None,
    sample: int = 0,
    seed: int = 42,
) -> Path:
    """Prepare prompts, manifests, and isolated agent directories for an Antigravity run."""
    if variant not in ALL_ANTIGRAVITY_VARIANTS:
        raise SystemExit(f"Unknown variant {variant!r}. Choose from {ALL_ANTIGRAVITY_VARIANTS}")

    run_dir = RUNS / run_id
    if run_dir.exists():
        raise SystemExit(f"Прогон {run_id} уже существует: {rel(run_dir)}. Укажите другой --run.")

    cfg = cfg or {}
    gap_threshold_s = cfg.get("gap_threshold_s", 30)
    quote_match_threshold = cfg.get("quote_match_threshold", 0.9)
    include_facts = cfg.get("include_facts", True)
    agents = cfg.get("agents", ["claude", "antigravity"])

    doc = ck.load(checklist_ref)
    selected_ids = _select_ids(dataset, ids, preset, sample, seed, split)

    manifest = {
        "run_id": run_id,
        "created_at": now_iso(),
        "mode": "antigravity_fine_grained",
        "dataset": dataset,
        "checklist": ck.ref_of(doc),
        "checklist_hash": doc["meta"]["content_hash"],
        "variant": variant,
        "params": {
            "gap_threshold_s": gap_threshold_s,
            "quote_match_threshold": quote_match_threshold,
            "include_facts": include_facts,
            "preset": preset,
            "sample_size": len(selected_ids),
        },
        "params_hash": stable_hash({
            "variant": variant,
            "gap_threshold_s": gap_threshold_s,
            "include_facts": include_facts,
        }),
        "agents": agents,
        "token_method": tokens.METHOD,
        "conversations": {},
        "token_projection": {},
    }

    # Calculate token projections for all variants across selected conversations
    all_variants_to_project = ["v1", *ALL_ANTIGRAVITY_VARIANTS]
    projections = {v: {"calls": 0, "tokens_in": 0} for v in all_variants_to_project}

    for cid in selected_ids:
        conv = cv.load(dataset, cid)
        facts = cv.facts(conv, gap_threshold_s)

        # Save S2 features
        save_json(
            run_dir / "features" / f"{cid}.json",
            {"conversation_id": cid, "operator_name_guess": cv.guess_operator_name(conv), **facts}
        )

        # 1. Project v1 baseline
        import cqa.prompts as base_prompts
        built_v1 = base_prompts.build("v1", doc, conv, facts, "v1", include_facts)
        projections["v1"]["calls"] += len(built_v1)
        projections["v1"]["tokens_in"] += sum(tokens.count(b["text"]) for b in built_v1)

        # 2. Project Antigravity variants
        for v in ALL_ANTIGRAVITY_VARIANTS:
            built_v = build_prompts(v, doc, conv, facts, dossier=None, include_facts=include_facts)
            projections[v]["calls"] += len(built_v)
            projections[v]["tokens_in"] += sum(tokens.count(b["text"]) for b in built_v)

        # 3. Write actual prompts for the chosen variant
        active_prompts = build_prompts(variant, doc, conv, facts, dossier=None, include_facts=include_facts)
        entries = []
        (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
        for b in active_prompts:
            filename = f"{cid}.md" if b["part"] in ("all", "facts") else f"{cid}__{b['part']}.md"
            (run_dir / "prompts" / filename).write_text(b["text"], encoding="utf-8")
            entries.append({
                "part": b["part"],
                "file": filename,
                "prompt_id": b["prompt_id"],
                "criteria_ids": b["criteria_ids"],
                "tokens_in": tokens.count(b["text"]),
                "template_hash": b["template_hash"],
            })

        manifest["conversations"][cid] = {
            "channel": conv.get("channel"),
            "language": conv.get("language"),
            "parts": entries,
        }

    manifest["token_projection"] = projections

    # Populate agent inboxes
    for agent in agents:
        agent_box = run_dir / "agents" / agent
        shutil.copytree(run_dir / "prompts", agent_box / "prompts")
        (agent_box / "responses").mkdir(parents=True, exist_ok=True)

        instructions = f"""# Инструкция для агента ({agent}) — Прогон `{run_id}`
Вариант декомпозиции: **{variant}**

1. Открой файлы в `prompts/`.
2. Для каждого файла скопируй его в чат облачной модели (или вызови CLI).
3. Ответ модели (строго JSON) сохрани в `responses/<то же имя без .md>.json` (или `.md`).
4. Запусти ingest: `python3 -m variants.antigravity ingest --run {run_id}`
"""
        (agent_box / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")

    save_json(run_dir / "manifest.json", manifest)
    log_event("antigravity_run_prepared", run_id=run_id, variant=variant, dialogs=len(selected_ids))
    return run_dir


def _read_agent_response(box: Path, stem: str) -> Tuple[Optional[str], Optional[dict], Optional[Path]]:
    for ext in (".json", ".md", ".txt"):
        p = box / "responses" / f"{stem}{ext}"
        if p.exists() and p.read_text(encoding="utf-8").strip():
            usage_file = box / "responses" / f"{stem}.usage.json"
            usage = load_json(usage_file) if usage_file.exists() else None
            return p.read_text(encoding="utf-8"), usage, p
    return None, None, None


def ingest(run_id: str, cfg: Optional[dict] = None, agents: Optional[List[str]] = None) -> dict:
    """Ingest responses, validate evidence, reduce sub-questions, and score dialogs."""
    run_dir = RUNS / run_id
    manifest = load_json(run_dir / "manifest.json")
    doc = ck.load(manifest["checklist"].replace("@v", "@"))
    crit_map = {c["id"]: c for c in ck.active_criteria(doc)}
    threshold = manifest["params"]["quote_match_threshold"]
    variant = manifest["variant"]

    summary = {}

    for agent in agents or manifest["agents"]:
        box = run_dir / "agents" / agent
        stats = {"done": 0, "pending": 0, "partial": 0, "ok": 0, "warn": 0, "error": 0}

        for cid, info in manifest["conversations"].items():
            conv = cv.load(manifest["dataset"], cid)
            turns = {t["idx"]: t for t in conv["transcript"]}

            merged_answers = {}
            merged_criteria = {}
            issues = []
            usage_rows = []
            found_count = 0

            for part in info["parts"]:
                stem = Path(part["file"]).stem
                raw, usage, path = _read_agent_response(box, stem)
                if raw is None:
                    continue
                found_count += 1

                parsed_json, err = base_parse(raw)
                tok_in = (usage or {}).get("input_tokens") or part["tokens_in"]
                tok_out = (usage or {}).get("output_tokens") or tokens.count(raw)
                usage_rows.append({
                    "part": part["part"],
                    "file": part["file"],
                    "tokens_in": tok_in,
                    "tokens_out": tok_out,
                    "exact": bool(usage),
                    "model": (usage or {}).get("model"),
                })

                if err:
                    issues.append({"level": "error", "criterion_id": None, "msg": f"[{part['part']}] {err}"})
                    continue

                # 1. Processing for v3_focus / v4_focus (one criterion per part)
                if part["part"] in crit_map:
                    cid_part = part["part"]
                    c = crit_map[cid_part]
                    crit_obj = parsed_json.get("criterion") or parsed_json
                    ans_raw = crit_obj.get("answer")

                    norm_ans, problem = normalize_answer(ans_raw, c)
                    if problem:
                        issues.append({"level": "error" if norm_ans is None else "warn", "criterion_id": cid_part, "msg": problem})
                    if norm_ans is not None:
                        merged_answers[cid_part] = norm_ans

                    # Validate evidence
                    ev_checks = []
                    for ev in crit_obj.get("evidence") or []:
                        idx = ev.get("idx")
                        turn = turns.get(idx)
                        if turn is None:
                            issues.append({"level": "error", "criterion_id": cid_part, "msg": f"Реплики #{idx} нет в диалоге"})
                            ev_checks.append({"idx": idx, "exists": False})
                        else:
                            cov = round(quote_coverage(ev.get("quote", ""), turn["text"]), 3)
                            ok = cov >= threshold
                            if not ok:
                                issues.append({"level": "warn", "criterion_id": cid_part, "msg": f"Цитата не совпадает с #{idx} (покрытие {cov})"})
                            ev_checks.append({"idx": idx, "exists": True, "quote_coverage": cov, "quote_ok": ok})

                    merged_criteria[cid_part] = {
                        **crit_obj,
                        "answer_normalized": norm_ans,
                        "evidence_checks": ev_checks,
                    }

                # 2. Processing for v_subq (atomic sub-questions per criterion)
                elif variant == "v_subq" and part["part"] in crit_map:
                    cid_part = part["part"]
                    c = crit_map[cid_part]
                    sq_list = parsed_json.get("sub_questions", [])
                    sq_dict = {}
                    for item in sq_list:
                        sq_id = item.get("id")
                        sq_dict[sq_id] = item.get("value")

                    # Deterministic reduction
                    deduced_ans, reduction_meta = reduce_subquestions(c, sq_dict)
                    norm_ans, problem = normalize_answer(deduced_ans, c)
                    if norm_ans is not None:
                        merged_answers[cid_part] = norm_ans

                    merged_criteria[cid_part] = {
                        "criterion_id": cid_part,
                        "answer": norm_ans,
                        "answer_normalized": norm_ans,
                        "sub_questions": sq_list,
                        "reduction_meta": reduction_meta,
                        "synthesized_answer": parsed_json.get("synthesized_answer"),
                        "explanation": parsed_json.get("explanation"),
                    }

                # 3. Processing for monolithic / bundled (all criteria in 1 part)
                elif part["part"] in ("all", "bundled"):
                    crit_list = parsed_json.get("criteria", [])
                    for item in crit_list:
                        c_id = item.get("criterion_id")
                        c = crit_map.get(c_id)
                        if not c:
                            continue

                        # Check if sub-questions with IDs are present in bundled output
                        if item.get("sub_questions") and any(isinstance(sq, dict) and "id" in sq for sq in item["sub_questions"]):
                            sq_dict = {sq["id"]: sq.get("value") for sq in item["sub_questions"] if isinstance(sq, dict) and "id" in sq}
                            deduced_ans, reduction_meta = reduce_subquestions(c, sq_dict)
                            # Prefer explicit answer if given, but record deduced
                            chosen_ans = item.get("answer") if item.get("answer") is not None else deduced_ans
                            norm_ans, problem = normalize_answer(chosen_ans, c)
                            if problem:
                                issues.append({"level": "warn", "criterion_id": c_id, "msg": problem})
                            if norm_ans is not None:
                                merged_answers[c_id] = norm_ans
                            item["reduction_meta"] = reduction_meta
                        else:
                            norm_ans, problem = normalize_answer(item.get("answer"), c)
                            if problem:
                                issues.append({"level": "error" if norm_ans is None else "warn", "criterion_id": c_id, "msg": problem})
                            if norm_ans is not None:
                                merged_answers[c_id] = norm_ans

                        # Evidence checks
                        ev_checks = []
                        for ev in item.get("evidence") or []:
                            idx = ev.get("idx")
                            turn = turns.get(idx)
                            if turn is None:
                                issues.append({"level": "error", "criterion_id": c_id, "msg": f"Реплики #{idx} нет в диалоге"})
                                ev_checks.append({"idx": idx, "exists": False})
                            else:
                                cov = round(quote_coverage(ev.get("quote", ""), turn["text"]), 3)
                                ok = cov >= threshold
                                ev_checks.append({"idx": idx, "exists": True, "quote_coverage": cov, "quote_ok": ok})

                        merged_criteria[c_id] = {
                            **item,
                            "answer_normalized": norm_ans,
                            "evidence_checks": ev_checks,
                        }

            if found_count == 0:
                stats["pending"] += 1
                continue

            stats["done"] += 1
            if found_count < len(info["parts"]):
                stats["partial"] += 1
                issues.append({"level": "warn", "criterion_id": None, "msg": f"Получено {found_count} из {len(info['parts'])} частей"})

            status = "error" if any(i["level"] == "error" for i in issues) else ("warn" if issues else "ok")
            stats[status] += 1

            # Compute score using official checklist formula
            scored = scoring.score(doc, merged_answers)

            result_doc = {
                "run_id": run_id,
                "agent": agent,
                "conversation_id": cid,
                "checklist": manifest["checklist"],
                "variant": manifest["variant"],
                "status": status,
                "usage": usage_rows,
                "score": scored,
                "answers": merged_answers,
                "criteria": merged_criteria,
                "issues": issues,
                "dialog_facts": parsed_json.get("dialog_facts") if isinstance(parsed_json, dict) else None,
                "overall_comment": parsed_json.get("overall_comment") if isinstance(parsed_json, dict) else None,
                "meta": parsed_json.get("meta") if isinstance(parsed_json, dict) else None,
            }

            dest = run_dir / "validated" / agent / f"{cid}.json"
            save_json(dest, result_doc)

        summary[agent] = stats
        log_event("antigravity_responses_ingested", run_id=run_id, agent=agent, **stats)

    return summary


def simulate(run_id: str, agent: str = "antigravity") -> int:
    """Generate high-fidelity synthetic mock responses for a prepared run.

    Enables instant dry-runs, end-to-end verification, and test execution without LLM calls.
    """
    run_dir = RUNS / run_id
    manifest = load_json(run_dir / "manifest.json")
    doc = ck.load(manifest["checklist"].replace("@v", "@"))
    crit_map = {c["id"]: c for c in ck.active_criteria(doc)}
    variant = manifest["variant"]

    box = run_dir / "agents" / agent
    (box / "responses").mkdir(parents=True, exist_ok=True)
    count_written = 0

    for cid, info in manifest["conversations"].items():
        conv = cv.load(manifest["dataset"], cid)
        first_op = next((t for t in conv["transcript"] if t["speaker"] == "operator"), None)
        last_op = next((t for t in reversed(conv["transcript"]) if t["speaker"] == "operator"), None)
        op_idx = first_op["idx"] if first_op else 1
        op_text = first_op["text"] if first_op else "Здравствуйте!"
        last_idx = last_op["idx"] if last_op else op_idx
        last_text = last_op["text"] if last_op else op_text

        for part in info["parts"]:
            p_id = part["part"]
            stem = Path(part["file"]).stem

            if variant in ("v3_focus", "v4_focus") and p_id in crit_map:
                c = crit_map[p_id]
                ans = "yes"
                if c["type"] == "scale":
                    ans = 20 if p_id == "c04" else 10
                elif c["type"] == "flag":
                    ans = "no"

                data = {
                    "meta": {"conversation_id": cid, "criterion_id": p_id},
                    "criterion": {
                        "criterion_id": p_id,
                        "applicability": {"na_allowed": c.get("na_allowed", False), "na_condition_met": False, "reasoning": "Критерий применим"},
                        "rule_applied": "Стандартное выполнение требований",
                        "alternatives_rejected": "Оснований для снижения или N/A нет",
                        "answer": ans,
                        "evidence": [{"idx": op_idx, "quote": op_text}],
                        "checked_range": None,
                        "explanation": f"{c['title']} — {ans}. Требование выполнено. Основание — реплика #{op_idx}.",
                        "confidence": 0.95,
                    },
                }
                save_json(box / "responses" / f"{stem}.json", data)
                count_written += 1

            elif variant == "v_subq" and p_id in crit_map:
                c = crit_map[p_id]
                sqs = derive_subquestions(c)
                sq_answers = []
                for s in sqs:
                    # Satisfy conditions cleanly
                    val = True
                    if s.get("kind") == "na_gate":
                        val = False  # Not N/A
                    elif s.get("is_violation"):
                        val = False  # No gross error
                    sq_answers.append({
                        "id": s["id"],
                        "value": val,
                        "evidence_idx": op_idx,
                        "quote": op_text,
                        "reasoning": "Подтверждено транскриптом",
                    })

                ans = "yes" if c["type"] != "scale" else (20 if p_id == "c04" else 10)
                if c["type"] == "flag":
                    ans = "no"

                data = {
                    "meta": {"conversation_id": cid, "criterion_id": p_id},
                    "sub_questions": sq_answers,
                    "synthesized_answer": ans,
                    "explanation": f"{c['title']} — {ans}. Все подвопросы подтверждены.",
                }
                save_json(box / "responses" / f"{stem}.json", data)
                count_written += 1

            elif variant in ("v4_mono", "v_subq_bundled", "v1") or p_id in ("all", "bundled"):
                criteria_out = []
                for c in ck.active_criteria(doc):
                    ans = "yes"
                    if c["type"] == "scale":
                        ans = 20 if c["id"] == "c04" else 10
                    elif c["type"] == "flag":
                        ans = "no"

                    sq_list = []
                    for sq in derive_subquestions(c):
                        v = True
                        if sq.get("kind") == "na_gate" or sq.get("is_violation"):
                            v = False
                        sq_list.append({"id": sq["id"], "value": v, "evidence_idx": op_idx, "quote": op_text})

                    criteria_out.append({
                        "criterion_id": c["id"],
                        "sub_questions": sq_list,
                        "answer": ans,
                        "evidence": [{"idx": op_idx, "quote": op_text}],
                        "checked_range": None,
                        "explanation": f"{c['title']} — {ans}. Основание — реплика #{op_idx}.",
                        "confidence": 0.95,
                    })

                data = {
                    "meta": {"conversation_id": cid},
                    "criteria": criteria_out,
                    "overall_comment": "Диалог проведён на высоком профессиональном уровне.",
                }
                save_json(box / "responses" / f"{stem}.json", data)
                count_written += 1

    return count_written


def report(run_id: str) -> dict:
    """Generate Markdown and CSV reports comparing variant performance and token economics."""
    run_dir = RUNS / run_id
    manifest = load_json(run_dir / "manifest.json")
    doc = ck.load(manifest["checklist"].replace("@v", "@"))
    crit = ck.active_criteria(doc)
    variant = manifest["variant"]

    rep_dir = run_dir / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)

    # Collect validated results
    results_by_agent: Dict[str, Dict[str, Any]] = {}
    for agent in manifest["agents"]:
        folder = run_dir / "validated" / agent
        if folder.exists():
            results_by_agent[agent] = {p.stem: load_json(p) for p in sorted(folder.glob("*.json"))}
        else:
            results_by_agent[agent] = {}

    # Write long table results.csv
    csv_rows = []
    for agent, conv_res in results_by_agent.items():
        for cid, r in conv_res.items():
            for row in r["score"]["rows"]:
                item = r["criteria"].get(row["criterion_id"], {})
                csv_rows.append([
                    run_id,
                    manifest["checklist"],
                    variant,
                    agent,
                    cid,
                    row["criterion_id"],
                    row["answer"],
                    row["points"],
                    row["effective_weight"],
                    row["effective_points"],
                    item.get("confidence"),
                    r["score"]["total"],
                    r["score"]["gross_error"],
                    r["status"],
                    item.get("explanation"),
                ])

    with open(rep_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "run_id", "checklist", "variant", "agent", "conversation_id", "criterion_id",
            "answer", "points", "effective_weight", "effective_points", "confidence",
            "total", "gross_error", "status", "explanation",
        ])
        w.writerows(csv_rows)

    # Calculate token & cost economics
    proj = manifest.get("token_projection", {})
    v1_tokens = proj.get("v1", {}).get("tokens_in", 1) or 1
    active_tokens = proj.get(variant, {}).get("tokens_in", 0)
    active_calls = proj.get(variant, {}).get("calls", 0)

    # Markdown summary
    md_lines = [
        f"# Отчёт по прогону `{run_id}` (Antigravity: {variant})",
        "",
        f"- **Чек-лист:** `{manifest['checklist']}`",
        f"- **Вариант декомпозиции:** `{variant}`",
        f"- **Диалогов в выборке:** {len(manifest['conversations'])}",
        f"- **Метод подсчёта токенов:** `{manifest.get('token_method')}`",
        "",
        "## Экономика токенов и проекция стоимости",
        "",
        "| Вариант | Вызовов LLM | Входных токенов | Мультипликатор к V1 | Gemini 1.5 Flash ($) | GPT-4o ($) | Claude 3.5 Sonnet ($) |",
        "|---|---|---|---|---|---|---|",
    ]

    for v_key in ["v1", "v3_focus", "v4_mono", "v4_focus", "v_subq", "v_subq_bundled"]:
        if v_key not in proj:
            continue
        p_info = proj[v_key]
        tin = p_info["tokens_in"]
        ncalls = p_info["calls"]
        mult = f"×{tin / v1_tokens:.1f}" if v1_tokens else "1.0"
        cost_gemini = f"${tin / 1e6 * MODEL_PRICES['gemini_flash']['input_per_mtok']:.4f}"
        cost_gpt = f"${tin / 1e6 * MODEL_PRICES['gpt4o']['input_per_mtok']:.3f}"
        cost_claude = f"${tin / 1e6 * MODEL_PRICES['claude_sonnet']['input_per_mtok']:.3f}"
        active_mark = " **(Текущий)**" if v_key == variant else ""
        md_lines.append(f"| `{v_key}`{active_mark} | {ncalls} | {tin:,} | {mult} | {cost_gemini} | {cost_gpt} | {cost_claude} |")

    md_lines.extend([
        "",
        "## Статус обработки диалогов по агентам",
        "",
        "| Агент | Готово | С предупреждением | С ошибкой | Средний балл | Нарушений (флаг) |",
        "|---|---|---|---|---|---|",
    ])

    for agent, conv_res in results_by_agent.items():
        done = len(conv_res)
        warns = sum(1 for r in conv_res.values() if r["status"] == "warn")
        errs = sum(1 for r in conv_res.values() if r["status"] == "error")
        scores = [r["score"]["total"] for r in conv_res.values() if r["score"]["total"] is not None]
        avg_score = f"{sum(scores) / len(scores):.2f}" if scores else "—"
        gross_count = sum(1 for r in conv_res.values() if r["score"].get("gross_error"))
        md_lines.append(f"| **{agent}** | {done} | {warns} | {errs} | {avg_score} | {gross_count} |")

    md_lines.extend([
        "",
        "## Результаты по критериям (выборка диалогов)",
        "",
        "| Диалог | " + " | ".join(c["id"] for c in crit) + " | Итог | Грубая ошибка |",
        "|---| " + " | ".join(["---"] * len(crit)) + " |---|---|",
    ])

    # Show first agent table
    first_agent = manifest["agents"][0]
    agent_data = results_by_agent.get(first_agent, {})
    for cid in manifest["conversations"]:
        r = agent_data.get(cid)
        if not r:
            continue
        ans_row = [str(r["answers"].get(c["id"], "—")) for c in crit]
        total_str = f"{r['score']['total']:.1f}" if r["score"]["total"] is not None else "N/A"
        gross_str = "Да" if r["score"]["gross_error"] else "Нет"
        md_lines.append(f"| `{cid}` | " + " | ".join(ans_row) + f" | **{total_str}** | {gross_str} |")

    report_text = "\n".join(md_lines)
    (rep_dir / "summary.md").write_text(report_text, encoding="utf-8")

    return {
        "report_file": rel(rep_dir / "summary.md"),
        "results_csv": rel(rep_dir / "results.csv"),
        "active_variant": variant,
        "tokens_in": active_tokens,
        "calls": active_calls,
    }
