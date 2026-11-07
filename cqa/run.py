"""Run orchestration in manual mode.

prepare: S1–S3 → runs/<run_id>/ with prompts for every agent (isolated folders) + manifest.
ingest:  S5–S7 → parse pasted answers, validate, score; results in runs/<run_id>/validated/.
"""

from __future__ import annotations

import random
import shutil
from collections import Counter
from pathlib import Path

from . import checklist as ck
from . import conversation as cv
from . import prompts as pr
from . import responses as rs
from . import common, scoring, tokens, versions
from .common import CQAError, load_json, log_event, now_iso, rel, save_json, stable_hash

AGENT_INSTRUCTIONS = """# Инструкция для агента ({agent})

Эта папка — изолированный «почтовый ящик» прогона `{run_id}` для агента **{agent}**.

## Ручной режим
1. Открой файл из `prompts/`, скопируй его целиком в **новый** чат модели.
2. Ответ модели целиком вставь в `responses/<то же имя>.md` (или `.json`).
3. Если интерфейс показывает расход токенов — создай рядом `responses/<то же имя>.usage.json`:
   `{{"input_tokens": 0, "output_tokens": 0, "model": "название модели"}}`.

## Режим агента (Claude Code / Codex / Antigravity, открытые в этой папке)
Обработай каждый файл из `prompts/` **независимо**, как отдельный запрос: прочитай файл, выполни
инструкцию из него и сохрани ответ (только JSON) в `responses/<то же имя>.json`.
Не читай другие папки, не используй ответы других диалогов, не меняй файлы в `prompts/`.
"""


def _select(dataset: str, ids: list, sample: int, seed: int) -> list:
    all_ids = cv.list_ids(dataset)
    if ids:
        missing = sorted(set(ids) - set(all_ids))
        if missing:
            raise CQAError(f"Нет таких диалогов: {missing}", "not_found")
        return ids
    if sample:
        rng = random.Random(seed)
        by_group = {}
        for cid in all_ids:
            by_group.setdefault(cid.rsplit("-", 1)[0], []).append(cid)
        picked = []
        for group in sorted(by_group):  # stratified by prefix (p1-ru, p1-kz, p1-en, p4-ru)
            k = max(1, round(sample * len(by_group[group]) / len(all_ids)))
            picked += rng.sample(by_group[group], min(k, len(by_group[group])))
        return sorted(picked)
    return all_ids


def prepare(run_id: str, dataset: str, checklist_ref: str, variant: str, cfg: dict,
            ids: list = None, sample: int = 0, seed: int = 42, author: str = "system") -> Path:
    run_dir = common.RUNS / run_id
    if run_dir.exists():
        raise CQAError(f"Прогон {run_id} уже существует: {rel(run_dir)}. Выберите другой --run.", "conflict")
    doc = ck.load(checklist_ref)
    template = cfg["template"]
    include_facts = cfg.get("include_facts", True)
    agents = cfg["agents"]
    params = {k: cfg[k] for k in ("gap_threshold_s", "quote_match_threshold", "include_facts", "template")}
    selected = _select(dataset, ids or [], sample, seed)

    manifest = {
        "run_id": run_id, "created_at": now_iso(), "mode": "manual", "dataset": dataset,
        "checklist": ck.ref_of(doc), "checklist_hash": doc["meta"]["content_hash"],
        "template": template, "variant": variant, "params": params, "params_hash": stable_hash(params),
        "agents": agents, "token_method": tokens.METHOD, "conversations": {}, "token_projection": {},
        "versions": versions.bundle(doc, cfg, template, author),
    }
    projection = {v: {"calls": 0, "tokens_in": 0} for v in pr.VARIANTS}

    for cid in selected:
        conv = cv.load(dataset, cid)
        facts = cv.facts(conv, cfg["gap_threshold_s"])
        save_json(run_dir / "features" / f"{cid}.json",
                  {"conversation_id": cid, "operator_name_guess": cv.guess_operator_name(conv), **facts})
        for v in pr.VARIANTS:  # token projection for every decomposition, only `variant` is written
            built = pr.build(template, doc, conv, facts, v, include_facts)
            projection[v]["calls"] += len(built)
            projection[v]["tokens_in"] += sum(tokens.count(b["text"]) for b in built)
            if v != variant:
                continue
            entries = []
            for b in built:
                name = cid if b["part"] == "all" else f"{cid}__{b['part']}"
                (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
                (run_dir / "prompts" / f"{name}.md").write_text(b["text"], encoding="utf-8")
                entries.append({"part": b["part"], "file": f"{name}.md", "prompt_id": b["prompt_id"],
                                "criteria_ids": b["criteria_ids"], "tokens_in": tokens.count(b["text"]),
                                "template_hash": b["template_hash"]})
            manifest["conversations"][cid] = {"channel": conv["channel"], "language": conv["language"],
                                              "parts": entries}
    manifest["token_projection"] = projection

    for agent in agents:
        box = run_dir / "agents" / agent
        shutil.copytree(run_dir / "prompts", box / "prompts")
        (box / "responses").mkdir(parents=True, exist_ok=True)
        (box / "INSTRUCTIONS.md").write_text(AGENT_INSTRUCTIONS.format(agent=agent, run_id=run_id), encoding="utf-8")

    save_json(run_dir / "manifest.json", manifest)
    log_event("run_prepared", actor=author, run_id=run_id, checklist_hash=manifest["checklist_hash"],
              versions=manifest["versions"], dataset=dataset, checklist=manifest["checklist"],
              variant=variant, template=template, params_hash=manifest["params_hash"],
              conversations=len(selected), agents=agents)
    return run_dir


def _read_response(box: Path, name: str):
    for ext in (".json", ".md", ".txt"):
        p = box / "responses" / f"{name}{ext}"
        if p.exists() and p.read_text(encoding="utf-8").strip():
            usage_file = box / "responses" / f"{name}.usage.json"
            usage = load_json(usage_file) if usage_file.exists() else None
            return p.read_text(encoding="utf-8"), usage, p
    return None, None, None


def ingest(run_id: str, cfg: dict, agents: list = None) -> dict:
    run_dir = common.RUNS / run_id
    manifest = load_json(run_dir / "manifest.json")
    doc = ck.load(manifest["checklist"].replace("@v", "@"))
    threshold = manifest["params"]["quote_match_threshold"]
    summary = {}

    for agent in agents or manifest["agents"]:
        box = run_dir / "agents" / agent
        stats = {"done": 0, "pending": 0, "partial": 0, "ok": 0, "warn": 0, "error": 0}
        for cid, info in manifest["conversations"].items():
            conv = cv.load(manifest["dataset"], cid)
            merged = {"answers": {}, "criteria": {}, "issues": [], "dialog_facts": None,
                      "model_total_estimate": None, "overall_comment": None, "meta": None}
            usage_rows, found = [], 0
            for part in info["parts"]:
                name = Path(part["file"]).stem
                raw, usage, path = _read_response(box, name)
                if raw is None:
                    continue
                found += 1
                data, err = rs.parse(raw)
                tokens_out = (usage or {}).get("output_tokens") or tokens.count(raw)
                tokens_in = (usage or {}).get("input_tokens") or part["tokens_in"]
                usage_rows.append({"part": part["part"], "tokens_in": tokens_in, "tokens_out": tokens_out,
                                   "exact": bool(usage), "model": (usage or {}).get("model"),
                                   "response_file": rel(path)})
                if err:
                    merged["issues"].append({"level": "error", "criterion_id": None, "msg": f"[{part['part']}] {err}"})
                    continue
                v = rs.validate(conv, doc, data, threshold, part["criteria_ids"])
                merged["answers"].update(v["answers"])
                merged["criteria"].update(v["criteria"])
                merged["issues"] += v["issues"]
                for key in ("dialog_facts", "model_total_estimate", "overall_comment", "meta"):
                    merged[key] = merged[key] or v[key]
            if found == 0:
                stats["pending"] += 1
                continue
            stats["done"] += 1
            if found < len(info["parts"]):
                stats["partial"] += 1
                merged["issues"].append({"level": "warn", "criterion_id": None,
                                         "msg": f"получено {found} из {len(info['parts'])} частей"})
            status = ("error" if any(i["level"] == "error" for i in merged["issues"])
                      else "warn" if merged["issues"] else "ok")
            stats[status] += 1
            result = {"run_id": run_id, "agent": agent, "conversation_id": cid,
                      "checklist": manifest["checklist"], "variant": manifest["variant"], "status": status,
                      "usage": usage_rows, "score": scoring.score(doc, merged["answers"]), **merged}
            save_json(run_dir / "validated" / agent / f"{cid}.json", result)
        summary[agent] = stats
        log_event("responses_ingested", run_id=run_id, agent=agent, **stats)
    return summary


# --- platform helpers: prompts and responses through the API, providers, dataset import ------------

def _manifest(run_id: str) -> dict:
    path = common.RUNS / run_id / "manifest.json"
    if not path.exists():
        raise CQAError(f"run {run_id} not found", "not_found")
    return load_json(path)


def status(run_id: str) -> dict:
    """Which prompts of a run still wait for a response, per agent."""
    m = _manifest(run_id)
    out = {"run_id": run_id, "checklist": m["checklist"], "variant": m["variant"], "agents": {}}
    for agent in m["agents"]:
        box = common.RUNS / run_id / "agents" / agent
        waiting, done = [], 0
        for cid, info in m["conversations"].items():
            for part in info["parts"]:
                name = Path(part["file"]).stem
                if _read_response(box, name)[0] is None:
                    waiting.append(name)
                else:
                    done += 1
        out["agents"][agent] = {"done": done, "waiting": len(waiting), "next": waiting[:10]}
    return out


def prompt_text(run_id: str, name: str) -> dict:
    m = _manifest(run_id)
    p = common.RUNS / run_id / "prompts" / f"{name}.md"
    if not p.exists():
        raise CQAError(f"prompt {name} not in run {run_id}", "not_found")
    return {"run_id": run_id, "name": name, "text": p.read_text(encoding="utf-8"), "agents": m["agents"]}


def submit_response(run_id: str, agent: str, name: str, text: str, usage: dict = None, actor: str = "system") -> dict:
    """Save a pasted model answer (what the frontend does instead of writing files by hand)."""
    m = _manifest(run_id)
    if agent not in m["agents"]:
        raise CQAError(f"agent {agent} is not part of run {run_id}")
    if not (common.RUNS / run_id / "prompts" / f"{name}.md").exists():
        raise CQAError(f"prompt {name} not in run {run_id}", "not_found")
    if not (text or "").strip():
        raise CQAError("empty response")
    box = common.RUNS / run_id / "agents" / agent / "responses"
    box.mkdir(parents=True, exist_ok=True)
    (box / f"{name}.md").write_text(text, encoding="utf-8")
    if usage:
        save_json(box / f"{name}.usage.json", usage)
    log_event("response_submitted", actor=actor, run_id=run_id, agent=agent, prompt=name,
              conversation_id=name.split("__")[0])
    return {"saved": rel(box / f"{name}.md")}


def dispatch(run_id: str, agent: str, provider: str = None, limit: int = 0, actor: str = "system") -> dict:
    """Button «Оценить» for a non-manual provider: send every waiting prompt of `agent` through the provider."""
    from . import providers

    m = _manifest(run_id)
    prov = providers.get(provider, agent=agent)
    box = common.RUNS / run_id / "agents" / agent
    results = Counter()
    sent = 0
    for cid, info in m["conversations"].items():
        for part in info["parts"]:
            name = Path(part["file"]).stem
            if _read_response(box, name)[0] is not None:
                continue
            if limit and sent >= limit:
                break
            res = prov.complete(box / "prompts" / f"{name}.md", box / "responses" / f"{name}.md")
            results[res["status"]] += 1
            sent += 1
            if prov.manual:
                break
    log_event("run_dispatched", actor=actor, run_id=run_id, agent=agent, provider=prov.name, results=dict(results))
    return {"provider": prov.describe(), "results": dict(results),
            "note": "manual provider: paste responses (run.submit) — nothing was sent" if prov.manual else None}


def import_dataset(dataset: str, records: list, actor: str = "system", source: str = "upload") -> dict:
    """MC.1 «Загрузить»: records in the organizers' API format → data/<dataset>/conversations/<id>.json."""
    folder = common.DATA / dataset.lower() / "conversations"
    ids = []
    for rec in records:
        if not rec.get("conversation_id") or not isinstance(rec.get("transcript"), list):
            raise CQAError("each record needs conversation_id and a transcript list")
        for k, t in enumerate(rec["transcript"], 1):
            if not {"timestamp", "speaker", "text"} <= set(t):
                raise CQAError(f"{rec['conversation_id']}: turn {k} needs timestamp, speaker, text")
            t.setdefault("idx", k)
        rec.setdefault("channel", "agent chat")
        rec.setdefault("audio_url", None)
        save_json(folder / f"{rec['conversation_id']}.json", rec)
        ids.append(rec["conversation_id"])
    log_event("dataset_imported", actor=actor, dataset=dataset, conversation_ids=ids, source=source)
    return {"dataset": dataset, "imported": ids}
