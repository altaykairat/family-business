"""Local FastAPI server for the transparent CQA experiment."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Invalid, score, validate_answers
from provider import draft, build_request, PROMPT_VERSION
from store import Store


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_env()
DATA_PATH = Path(os.environ.get("CQA_PROTOTYPE_DB", str(ROOT / "prototype" / "state.db")))
store = Store(DATA_PATH, ROOT / "data" / "checklists" / "default.json")
app = FastAPI(title="Transparent CQA prototype", version="0.1")
batch_pool = ThreadPoolExecutor(max_workers=1)
batch_jobs = {}


@app.exception_handler(Invalid)
async def invalid_handler(_request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"error": str(exc)})


class ImportRequest(BaseModel):
    records: list[dict]
    group_id: str = "UPLOAD"


class CreateEvaluation(BaseModel):
    conversation_id: str
    checklist_id: str = "default"
    checklist_version: int | None = None


class AppealRequest(BaseModel):
    criterion_id: str
    comment: str


class DecisionRequest(BaseModel):
    action: str
    reason: str
    answer: dict | None = None


class GroupRequest(BaseModel):
    group_id: str = Field(min_length=1, max_length=100)


class BatchRequest(BaseModel):
    conversation_ids: list[str] = Field(min_length=1, max_length=100)
    checklist_id: str = "default"


@app.get("/")
def home():
    return FileResponse(ROOT / "prototype" / "web" / "index.html")


@app.get("/app.js")
def javascript():
    return FileResponse(ROOT / "prototype" / "web" / "app.js", media_type="text/javascript")


@app.get("/style.css")
def stylesheet():
    return FileResponse(ROOT / "prototype" / "web" / "style.css", media_type="text/css")


@app.get("/api/bootstrap")
def bootstrap():
    return {"checklist": store.latest_checklist(), "versions": store.checklist_versions(),
            "conversations": store.conversations(),
            "provider": os.environ.get("CQA_PROVIDER", "mock"),
            "ai_configured": bool(os.environ.get("CQA_PROVIDER", "mock") == "mock" or
                                  (os.environ.get("CQA_LLM_API_KEY") and os.environ.get("CQA_LLM_MODEL")))}


@app.post("/api/import")
def import_records(body: ImportRequest):
    return store.import_conversations(body.records, body.group_id)


@app.post("/api/import-dev")
def import_dev():
    records = [json.loads(path.read_text(encoding="utf-8")) for path in
               sorted((ROOT / "data" / "dev" / "conversations").glob("*.json"))]
    return store.import_conversations(records, "DEV")


@app.post("/api/import-group")
def import_group(body: GroupRequest):
    source = os.environ.get("CQA_API_URL", "").strip()
    if not source:
        raise Invalid("Set CQA_API_URL in .env for organizer dataset import")
    parts = urlsplit(source)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "group_id"]
    query.append(("group_id", body.group_id))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    try:
        with urlopen(url, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise Invalid(f"Dataset download failed: {type(exc).__name__}") from exc
    if payload.get("isSuccess") is not True or not isinstance(payload.get("data", {}).get("records"), list):
        raise Invalid("Organizer API did not return records")
    return store.import_conversations(payload["data"]["records"], body.group_id)


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str):
    return {"conversation": store.conversation(cid), "evaluations": store.evaluations_for(cid)}


@app.get("/api/checklists/{cid}")
def get_checklist(cid: str):
    return store.latest_checklist(cid)


@app.post("/api/checklists")
def save_checklist(body: dict):
    return store.save_checklist(body)


def evaluation_view(eid: str):
    evaluation = store.evaluation(eid)
    conversation = store.conversation(evaluation["conversation_id"])
    errors = validate_answers(evaluation["checklist_snapshot"], conversation, evaluation["answers"])
    baseline_event = next((event for event in reversed(evaluation["events"])
                           if event["kind"] == "ai_draft" and event["actor"] == evaluation["source"]), None)
    baseline_answers = baseline_event["detail"]["answers"] if baseline_event else None
    baseline_errors = (validate_answers(evaluation["checklist_snapshot"], conversation, baseline_answers)
                       if baseline_answers else [])
    baseline_score = (score(evaluation["checklist_snapshot"], conversation, baseline_answers)
                      if baseline_answers and not baseline_errors else None)
    return {**evaluation, "conversation": conversation, "validation_errors": errors,
            "score": None if errors else score(evaluation["checklist_snapshot"], conversation, evaluation["answers"]),
            "baseline_score": baseline_score, "baseline_errors": baseline_errors,
            "changed_criteria": ([cid for cid in evaluation["answers"]
                                  if evaluation["answers"][cid] != baseline_answers.get(cid)]
                                 if baseline_answers else [])}


@app.post("/api/evaluations")
def create_evaluation(body: CreateEvaluation):
    result = store.create_evaluation(body.conversation_id, body.checklist_id,
                                     checklist_version=body.checklist_version)
    return evaluation_view(result["id"])


@app.get("/api/evaluations/{eid}")
def get_evaluation(eid: str):
    return evaluation_view(eid)


@app.get("/api/evaluations/{eid}/prompt")
def prompt_preview(eid: str):
    evaluation = store.evaluation(eid)
    conversation = store.conversation(evaluation["conversation_id"])
    actual = next((event["detail"] for event in reversed(evaluation["events"])
                   if event["kind"] == "ai_draft" and event["detail"].get("request")), None)
    if actual:
        return {"prompt_version": actual["prompt_version"], "sent": True,
                "provider": actual["provider"], "request": actual["request"]}
    return {"prompt_version": PROMPT_VERSION, "sent": False,
            "provider": os.environ.get("CQA_PROVIDER", "mock"),
            "request": build_request(conversation, evaluation["checklist_snapshot"],
                                     os.environ.get("CQA_LLM_MODEL", "<model-not-configured>"),
                                     os.environ.get("CQA_PROVIDER", "mock"))}


@app.patch("/api/evaluations/{eid}/answers/{cid}")
def patch_answer(eid: str, cid: str, body: dict):
    store.patch_answer(eid, cid, body)
    return evaluation_view(eid)


@app.post("/api/evaluations/{eid}/ai")
def ai_evaluation(eid: str):
    evaluation = store.evaluation(eid)
    conversation = store.conversation(evaluation["conversation_id"])
    response = draft(conversation, evaluation["checklist_snapshot"])
    store.apply_ai_draft(eid, response)
    return evaluation_view(eid)


def run_batch(job_id, body):
    job = batch_jobs[job_id]
    for cid in body.conversation_ids:
        try:
            evaluation = store.create_evaluation(cid, body.checklist_id)
            response = draft(store.conversation(cid), evaluation["checklist_snapshot"])
            store.apply_ai_draft(evaluation["id"], response)
            view = evaluation_view(evaluation["id"])
            job["results"].append({"conversation_id": cid, "evaluation_id": evaluation["id"],
                                   "status": "valid" if not view["validation_errors"] else "needs_review",
                                   "score": view["score"]["total"] if view["score"] else None,
                                   "errors": view["validation_errors"]})
        except Exception as exc:
            job["results"].append({"conversation_id": cid, "status": "error", "error": str(exc)})
        job["done"] += 1
    job["status"] = "complete"


@app.post("/api/batch")
def batch(body: BatchRequest):
    if os.environ.get("CQA_PROVIDER", "mock") != "mock" and (
        not os.environ.get("CQA_LLM_API_KEY") or not os.environ.get("CQA_LLM_MODEL")
    ):
        raise Invalid("Configure AI before batch evaluation")
    if len(set(body.conversation_ids)) != len(body.conversation_ids):
        raise Invalid("Duplicate conversation IDs in batch")
    for cid in body.conversation_ids:
        store.conversation(cid)
    store.latest_checklist(body.checklist_id)
    job_id = uuid4().hex
    batch_jobs[job_id] = {"id": job_id, "status": "running", "total": len(body.conversation_ids),
                          "done": 0, "results": []}
    batch_pool.submit(run_batch, job_id, body)
    return batch_jobs[job_id]


@app.get("/api/batch/{job_id}")
def batch_status(job_id: str):
    if job_id not in batch_jobs:
        raise Invalid("Batch job not found in this server session")
    return batch_jobs[job_id]


@app.post("/api/evaluations/{eid}/appeals")
def open_appeal(eid: str, body: AppealRequest):
    store.appeal(eid, body.criterion_id, body.comment)
    return evaluation_view(eid)


@app.post("/api/appeals/{aid}/decision")
def decide_appeal(aid: str, body: DecisionRequest):
    result = store.decide_appeal(aid, body.model_dump(exclude_none=True))
    return evaluation_view(result["id"])
