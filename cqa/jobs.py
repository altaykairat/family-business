"""Background jobs for long button actions (the web UI must not hold a request for 15–40 minutes).

A job runs in a thread of the API server process; its progress lives in memory and every step is also written to
the append-only history (run_prepared, responses_ingested, evaluations published, results_exported …), so nothing
is lost if the server restarts — only the progress view is. Jobs start only by a button (decision 11).

kind "evaluate": dataset/ids → run.prepare → every prompt in its own isolated provider session (N in parallel)
                 → run.ingest → eval.import → (eval.publish) → (results.export set=…)
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from . import checklist as ck
from . import common, providers, run
from .common import CQAError

_JOBS: dict = {}
_LOCK = threading.Lock()


def _new(kind: str, actor: str, params: dict) -> dict:
    with _LOCK:
        job_id = f"job-{len(_JOBS) + 1:03d}-{datetime.now().strftime('%H%M%S')}"
        job = {"job_id": job_id, "kind": kind, "actor": actor, "params": params, "status": "running",
               "step": "queued", "done": 0, "total": 0, "failed": [], "log": [], "result": None, "error": None,
               "started_at": common.now_iso(), "finished_at": None, "seconds": 0}
        _JOBS[job_id] = job
        return job


def _log(job: dict, msg: str) -> None:
    job["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
    job["log"] = job["log"][-200:]


def status(job_id: Optional[str] = None) -> dict:
    if job_id:
        job = _JOBS.get(job_id)
        if not job:
            raise CQAError(f"job {job_id} not found (jobs live in the server process; restarted?)", "not_found")
        return _view(job)
    return {"jobs": [_view(j) for j in sorted(_JOBS.values(), key=lambda j: j["started_at"], reverse=True)]}


def _view(job: dict) -> dict:
    v = dict(job)
    v["seconds"] = round(time.time() - job["_t0"], 1) if job.get("_t0") and job["status"] == "running" else job["seconds"]
    v.pop("_t0", None)
    return v


def start_evaluate(actor: str, dataset: str = "dev", ids: Optional[list] = None, provider: str = "codex-cli",
                   template: str = "v2", checklist: str = "default", workers: int = 4, publish: bool = True,
                   export_set: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    from . import access

    access.require(actor, "evaluation.run")
    if publish:
        access.require(actor, "evaluation.publish")
    prov = providers.get(provider)
    if prov.manual or not prov.enabled() or prov.spec.get("detach"):
        raise CQAError(f"provider {provider} cannot run unattended (manual, disabled or an IDE chat)")
    workers = max(1, min(int(workers or 4), 8))
    run_id = run_id or f"ui-{dataset}-{datetime.now().strftime('%m%d-%H%M%S')}"
    params = {"dataset": dataset, "ids": ids, "provider": provider, "template": template, "checklist": checklist,
              "workers": workers, "publish": publish, "export_set": export_set, "run_id": run_id}
    job = _new("evaluate", actor, params)
    job["_t0"] = time.time()
    threading.Thread(target=_evaluate, args=(job, actor), daemon=True).start()
    return _view(job)


def _evaluate(job: dict, actor: str) -> None:
    p = job["params"]
    agent = p["provider"]
    try:
        job["step"] = "prepare"
        cfg = common.load_config()
        cfg["agents"] = [agent]
        cfg["template"] = p["template"]
        run.prepare(p["run_id"], p["dataset"], p["checklist"], "v1", cfg, ids=p["ids"], author=actor)
        box = common.RUNS / p["run_id"] / "agents" / agent
        prompts = sorted((box / "prompts").glob("*.md"))
        job["total"] = len(prompts)
        ref = ck.ref_of(ck.load(p["checklist"]))
        _log(job, f"prepared {len(prompts)} prompts · {ref} · template {p['template']}")

        job["step"] = "evaluate"
        prov = providers.get(p["provider"])

        def one(path):
            res = prov.complete(path, box / "responses" / f"{path.stem}.md")
            if res["status"] != "done":  # one retry: transient CLI / network failures
                res = prov.complete(path, box / "responses" / f"{path.stem}.md")
            return path.stem, res

        with ThreadPoolExecutor(max_workers=p["workers"]) as ex:
            for fut in as_completed([ex.submit(one, x) for x in prompts]):
                name, res = fut.result()
                job["done"] += 1
                if res["status"] == "done":
                    _log(job, f"{name} done in {res.get('seconds')} s")
                else:
                    job["failed"].append({"name": name, "status": res["status"], "error": res.get("error")})
                    _log(job, f"{name} {res['status']}: {res.get('error') or ''}"[:300])
        common.log_event("run_dispatched", actor=actor, run_id=p["run_id"], agent=agent, provider=p["provider"],
                         results={"done": job["done"] - len(job["failed"]), "failed": len(job["failed"])}, job=job["job_id"])

        job["step"] = "ingest"
        ing = run.ingest(p["run_id"], common.load_config(), [agent])[agent]
        _log(job, f"validated: ok {ing['ok']}, warn {ing['warn']}, error {ing['error']}, pending {ing['pending']}")
        from . import api  # late import: api imports this module

        imported = api.call("eval.import", actor, {"source": f"{p['run_id']}:{agent}"})["data"]
        result = {"run_id": p["run_id"], "checklist": ref, "validated": ing, "imported": imported["imported"],
                  "evaluation_ids": imported["evaluation_ids"], "failed": job["failed"]}
        if p["publish"] and imported["evaluation_ids"]:
            job["step"] = "publish"
            api.call("eval.publish", actor, {"evaluation_ids": imported["evaluation_ids"]})
            result["published"] = len(imported["evaluation_ids"])
            _log(job, f"published {result['published']}")
        if p["export_set"]:
            job["step"] = "export"
            ex = api.call("results.export", actor, {"set": p["export_set"], "dataset": p["dataset"]})["data"]
            result["export"] = {"path": ex["path"], "check": ex["check"]}
            _log(job, f"exported {ex['path']} · check {'ok' if ex['check']['ok'] else 'FAILED'}")
        job["result"] = result
        job["status"] = "done" if not job["failed"] else "done_with_errors"
    except Exception as e:  # the job must always end with a visible status
        job["status"] = "failed"
        job["error"] = getattr(e, "message", None) or str(e)
        _log(job, "FAILED: " + job["error"])
        traceback.print_exc()
    finally:
        job["step"] = "finished"
        job["finished_at"] = common.now_iso()
        job["seconds"] = round(time.time() - job.get("_t0", time.time()), 1)


def fetch_dataset(actor: str, group_id: str, dataset: Optional[str] = None) -> dict:
    """«Загрузить group_id»: records from the organizers' API (CQA_API_URL in .env) → data/<dataset>/."""
    import importlib.util

    from . import access

    access.require(actor, "dataset.manage")
    spec = importlib.util.spec_from_file_location("fetch_dataset", common.ROOT / "scripts" / "fetch_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        raw = mod.fetch(group_id)
    except SystemExit as e:
        raise CQAError(str(e))
    except Exception as e:
        raise CQAError(f"organizers' API: {e}")
    records = (raw.get("data") or {}).get("records") or raw.get("records") or []
    if not records:
        raise CQAError(f"no records for group_id={group_id}")
    return run.import_dataset((dataset or group_id).lower(), records, actor, source=f"api:{group_id}")
