"""Configurable chat-completions adapter; rubric and evidence stay visible."""

import json
import os
from time import sleep
from time import perf_counter
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr, ValidationError

from engine import Invalid, blank_answers, validate_answers


PROMPT_VERSION = "rubric-first-v2"


class ProviderFailure(Invalid):
    def __init__(self, message, trace):
        super().__init__(message)
        self.trace = trace


class CheckedRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_idx: StrictInt
    end_idx: StrictInt


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: StrictStr
    decision: StrictStr
    scale_score: StrictInt | StrictFloat | None
    explanation: StrictStr
    evidence_turn_indices: list[StrictInt]
    checked_turn_range: CheckedRange
    na_reason: StrictStr
    violation_type: StrictStr


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[ModelDecision]


def configured_model():
    return (os.environ.get("CQA_MODEL") or os.environ.get("CQA_LLM_MODEL") or "").strip()


def configured_key(provider):
    name = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    return (os.environ.get(name) or os.environ.get("CQA_LLM_API_KEY") or "").strip()


def bounded_int(name, default, low, high):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise Invalid(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise Invalid(f"{name} must be between {low} and {high}")
    return value


def output_schema(checklist):
    # Deliberately uses only the Structured Outputs subset: required fields,
    # additionalProperties=false, enums, arrays, and nullable primitive types.
    fields = {
        "criterion_id": {"type": "string", "enum": [c["id"] for c in checklist["criteria"]]},
        "decision": {"type": "string", "enum": ["yes", "no", "na", "scored"]},
        "scale_score": {"type": ["number", "null"]},
        "explanation": {"type": "string"},
        "evidence_turn_indices": {"type": "array", "items": {"type": "integer"}},
        "checked_turn_range": {"type": "object", "properties": {
            "start_idx": {"type": "integer"}, "end_idx": {"type": "integer"}},
            "required": ["start_idx", "end_idx"], "additionalProperties": False},
        "na_reason": {"type": "string"},
        "violation_type": {"type": "string"},
    }
    return {"type": "object", "properties": {"answers": {"type": "array", "items": {
        "type": "object", "properties": fields, "required": list(fields),
        "additionalProperties": False}}}, "required": ["answers"], "additionalProperties": False}


def parse_openai_output(parsed, checklist):
    try:
        items = ModelOutput.model_validate(parsed).answers
    except ValidationError as exc:
        raise Invalid("AI response does not match the evaluation schema") from exc
    known = {c["id"]: c for c in checklist["criteria"]}
    if len(items) != len(known) or {item.criterion_id for item in items} != set(known):
        raise Invalid("AI omitted, duplicated, or invented a criterion")
    answers = {}
    for item in items:
        criterion = known[item.criterion_id]
        kind = criterion["type"]
        if item.decision == "na":
            if not criterion.get("na_allowed") or item.scale_score is not None:
                raise Invalid(f"{item.criterion_id}: invalid N/A decision")
            answer, applicability = None, "na"
        elif kind == "scale":
            if item.decision != "scored" or item.scale_score not in {s["value"] for s in criterion["scale"]}:
                raise Invalid(f"{item.criterion_id}: invalid scale decision")
            answer, applicability = item.scale_score, "applicable"
        else:
            if item.decision not in {"yes", "no"} or item.scale_score is not None:
                raise Invalid(f"{item.criterion_id}: invalid binary/flag decision")
            answer, applicability = item.decision, "applicable"
        answers[item.criterion_id] = {
            "applicability": applicability, "answer": answer, "na_reason": item.na_reason,
            "evidence_idx": item.evidence_turn_indices,
            "checked_turn_range": item.checked_turn_range.model_dump(),
            "explanation": item.explanation, "violation_type": item.violation_type if kind == "flag" else "",
        }
    return answers


def estimate_cost(usage, provider):
    input_price = os.environ.get("CQA_INPUT_USD_PER_MILLION", "")
    output_price = os.environ.get("CQA_OUTPUT_USD_PER_MILLION", "")
    if not input_price or not output_price or not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens") if provider == "openai" else usage.get("promptTokenCount")
    output_tokens = usage.get("completion_tokens") if provider == "openai" else usage.get("candidatesTokenCount")
    if input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * float(input_price) + output_tokens * float(output_price)) / 1_000_000, 6)


def build_request(conversation, checklist, model, provider="openai"):
    numbered = "\n".join(
        f"#{m['idx']} [{m.get('timestamp', '')}] {m['speaker']}: {m['text']}"
        for m in conversation["transcript"]
    )
    instructions = (
        "You are auditing only the operator, not the bot or system. Read the complete "
        "conversation. Follow the supplied checklist literally for the language of the client. "
        "For every criterion, decide applicability, inspect the relevant turn range, "
        "select evidence turns, then apply exactly one allowed rubric step. "
        "Return one answer for every supplied criterion, with its exact criterion_id. "
        "decision is yes/no for binary and flag, scored for scale, or na only where allowed. "
        "For scored decisions set scale_score to the exact permitted step; otherwise null. "
        "For N/A provide a concrete na_reason; otherwise use an empty string. "
        "For a flag decision yes, name the detected violation in violation_type; "
        "otherwise use an empty string. Include concise, checkable explanation, "
        "evidence_turn_indices and checked_turn_range start_idx/end_idx. "
        "For a missing action, cite where it was expected. Use original turn indices. "
        "Do not calculate weighted points or invent transcript quotes or timestamps."
    )
    content = json.dumps({"conversation": numbered, "channel": conversation.get("channel"),
                          "language": conversation.get("language"),
                          "global_rules": checklist.get("global_rules", []),
                          "criteria": checklist["criteria"]}, ensure_ascii=False)
    chat = {"model": model, "messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": content}]}
    if provider == "openai":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "cqa_evaluation", "strict": True, "schema": output_schema(checklist)}}
        chat["max_completion_tokens"] = bounded_int("CQA_LLM_MAX_OUTPUT_TOKENS", 4096, 256, 32768)
    temperature = os.environ.get("CQA_LLM_TEMPERATURE", "").strip()
    if temperature:
        chat["temperature"] = float(temperature)
    if provider == "gemini":
        gemini = {"contents": [{"parts": [{"text": instructions + "\n\n" + content}]}]}
        if temperature:
            gemini["generationConfig"] = {"temperature": float(temperature)}
        return gemini
    return chat


def draft(conversation, checklist):
    provider = os.environ.get("CQA_PROVIDER", "mock").strip().lower()
    model = configured_model() or "mock-technical-demo"
    key = configured_key(provider)
    if provider == "mock":
        model = "mock-technical-demo"
        answers = blank_answers(checklist)
        return {"answers": answers, "model": model, "provider": "mock", "usage": None,
                "raw": json.dumps(answers, ensure_ascii=False), "duration_seconds": 0, "cost_usd": None,
                "prompt_version": PROMPT_VERSION, "request": None}
    if provider not in {"openai", "gemini"}:
        raise Invalid("CQA_PROVIDER must be mock, openai or gemini")
    if not key or model == "mock-technical-demo":
        raise Invalid("Set the provider API key and CQA_MODEL before real evaluation")
    payload = build_request(conversation, checklist, model, provider)
    headers = {"Content-Type": "application/json"}
    if provider == "openai":
        url = os.environ.get("CQA_LLM_URL", "").strip() or "https://api.openai.com/v1/chat/completions"
        headers["Authorization"] = f"Bearer {key}"
    else:
        url = os.environ.get("CQA_LLM_URL", "").strip() or (
            "https://generativelanguage.googleapis.com/v1beta/models/" + quote(model, safe="") + ":generateContent")
        headers["x-goog-api-key"] = key
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    started = perf_counter()
    text = None
    usage = None
    timeout = bounded_int("CQA_LLM_TIMEOUT_SECONDS", 45, 1, 180)
    retries = bounded_int("CQA_LLM_MAX_RETRIES", 1, 0, 2)
    def failure(message):
        return ProviderFailure(message, {"provider": provider, "model": model,
                                         "prompt_version": PROMPT_VERSION, "request": payload,
                                         "raw": text, "usage": usage,
                                         "duration_seconds": round(perf_counter() - started, 3),
                                         "error": message})
    try:
        for attempt in range(retries + 1):
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                    raise failure(f"Provider HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt == retries:
                    raise failure(f"Provider connection failed: {type(exc).__name__}") from exc
            sleep(min(2 ** attempt, 2))
        if provider == "openai":
            choice = raw["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise failure("AI refused to evaluate this conversation")
            if choice.get("finish_reason") not in {None, "stop"}:
                raise failure(f"AI response incomplete: {choice['finish_reason']}")
            text = message["content"]
        else:
            text = "".join(part.get("text", "") for part in raw["candidates"][0]["content"]["parts"])
        if not isinstance(text, str):
            raise failure("AI returned non-text content")
        parsed = json.loads(text)
        if provider == "openai" or (isinstance(parsed, dict) and isinstance(parsed.get("answers"), list)
                                     and parsed["answers"] and "decision" in parsed["answers"][0]):
            try:
                answers = parse_openai_output(parsed, checklist)
            except Invalid as exc:
                raise failure(str(exc)) from exc
        else:
            candidates = parsed["answers"]
            if not isinstance(candidates, list):
                raise failure("AI answers must be a list")
            known = {c["id"] for c in checklist["criteria"]}
            answers = {}
            for item in candidates:
                cid = item.get("criterion_id")
                if cid not in known or cid in answers:
                    raise failure("AI returned unknown or duplicate criterion")
                answers[cid] = {field: item.get(field) for field in
                                ("applicability", "na_reason", "answer", "evidence_idx", "explanation",
                                 "checked_turn_range", "violation_type") if field in item}
            if set(answers) != known:
                raise failure("AI omitted a criterion")
        errors = validate_answers(checklist, conversation, answers, require_checked_range=True)
        if errors:
            raise failure("AI evaluation failed validation: " + "; ".join(errors))
        usage = raw.get("usage") if provider == "openai" else raw.get("usageMetadata")
        return {"answers": answers, "model": model, "provider": provider, "request": payload,
                "usage": usage, "cost_usd": estimate_cost(usage, provider), "raw": text,
                "duration_seconds": round(perf_counter() - started, 3), "prompt_version": PROMPT_VERSION}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise failure("AI response is not valid evaluation JSON") from exc
