"""Configurable chat-completions adapter; rubric and evidence stay visible."""

import json
import os
from time import perf_counter
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine import Invalid


PROMPT_VERSION = "rubric-first-v1"


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
        "For every criterion, decide applicability first, then choose transcript evidence, "
        "then choose exactly one allowed answer. N/A requires the stated condition. "
        "Missing actions need evidence at the place they were expected. "
        "Return only JSON: {\"answers\": [{\"criterion_id\": string, "
        "\"applicability\": \"applicable\"|\"na\", \"answer\": \"yes\"|\"no\"|number|null, "
        "\"na_reason\": string, \"evidence_idx\": [integer], \"explanation\": string}]}. "
        "Use original transcript indices. Do not calculate weighted points."
    )
    content = json.dumps({"conversation": numbered, "channel": conversation.get("channel"),
                          "language": conversation.get("language"),
                          "global_rules": checklist.get("global_rules", []),
                          "criteria": checklist["criteria"]}, ensure_ascii=False)
    chat = {"model": model, "messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": content}]}
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
    model = os.environ.get("CQA_LLM_MODEL", "").strip() or "mock-technical-demo"
    key = os.environ.get("CQA_LLM_API_KEY", "").strip()
    if provider == "mock":
        model = "mock-technical-demo"
        evidence = next((m["idx"] for m in conversation["transcript"] if m["speaker"] == "operator"), 1)
        answers = {}
        for criterion in checklist["criteria"]:
            value = (min(step["value"] for step in criterion["scale"]) if criterion["type"] == "scale"
                     else "no")
            answers[criterion["id"]] = {"applicability": "applicable", "na_reason": "",
                                         "answer": value, "evidence_idx": [evidence],
                                         "explanation": "MOCK: техническая заглушка; критерий не оценивался моделью."}
        return {"answers": answers, "model": model, "provider": "mock", "usage": None,
                "raw": json.dumps(answers, ensure_ascii=False), "duration_seconds": 0, "cost_usd": None,
                "prompt_version": PROMPT_VERSION, "request": None}
    if provider not in {"openai", "gemini"}:
        raise Invalid("CQA_PROVIDER must be mock, openai or gemini")
    if not key or model == "mock-technical-demo":
        raise Invalid("Set CQA_LLM_API_KEY and CQA_LLM_MODEL for the selected provider")
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
    try:
        with urlopen(request, timeout=120) as response:
            raw = json.loads(response.read().decode("utf-8"))
        text = (raw["choices"][0]["message"]["content"] if provider == "openai"
                else "".join(part.get("text", "") for part in raw["candidates"][0]["content"]["parts"]))
        if not isinstance(text, str):
            raise Invalid("AI returned non-text content")
        parsed = json.loads(text)
        candidates = parsed["answers"]
        if not isinstance(candidates, list):
            raise Invalid("AI answers must be a list")
        known = {c["id"] for c in checklist["criteria"]}
        answers = {}
        for item in candidates:
            cid = item.get("criterion_id")
            if cid not in known or cid in answers:
                raise Invalid("AI returned unknown or duplicate criterion")
            answers[cid] = {field: item.get(field) for field in
                            ("applicability", "na_reason", "answer", "evidence_idx", "explanation")}
        if set(answers) != known:
            raise Invalid("AI omitted a criterion")
        usage = raw.get("usage") if provider == "openai" else raw.get("usageMetadata")
        return {"answers": answers, "model": model, "provider": provider, "request": payload,
                "usage": usage, "cost_usd": estimate_cost(usage, provider), "raw": text,
                "duration_seconds": round(perf_counter() - started, 3), "prompt_version": PROMPT_VERSION}
    except HTTPError as exc:
        raise Invalid(f"Provider HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise Invalid(f"Provider connection failed: {type(exc).__name__}") from exc
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise Invalid(f"AI response is not valid evaluation JSON: {exc}") from exc
