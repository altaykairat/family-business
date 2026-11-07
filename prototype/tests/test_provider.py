import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))
from engine import normalize_conversation
from provider import ProviderFailure, build_request, draft

CHECKLIST = json.loads((ROOT / "data/checklists/default.json").read_text(encoding="utf-8"))
CONVERSATION = normalize_conversation(json.loads(
    (ROOT / "data/dev/conversations/p1-en-001.json").read_text(encoding="utf-8")))


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps(self.value).encode("utf-8")


def model_answers(checklist=CHECKLIST):
    return [{"criterion_id": c["id"], "decision": "scored" if c["type"] == "scale" else
             "no" if c["type"] == "flag" else "yes",
             "scale_score": max(s["value"] for s in c["scale"]) if c["type"] == "scale" else None,
             "explanation": "Checked the relevant exchange.", "evidence_turn_indices": [1],
             "checked_turn_range": {"start_idx": 1, "end_idx": len(CONVERSATION["transcript"])},
             "na_reason": "", "violation_type": ""} for c in checklist["criteria"]]


def openai_response(answers=None, **message):
    content = json.dumps({"answers": model_answers() if answers is None else answers})
    return Response({"choices": [{"finish_reason": "stop", "message": {"content": content, **message}}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CQA_PROVIDER": "openai", "CQA_MODEL": "gpt-4o-mini",
                                        "OPENAI_API_KEY": "unused-test-key", "CQA_LLM_URL": "",
                                        "CQA_LLM_MAX_RETRIES": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_openai_structured_request_and_valid_response(self):
        with patch("provider.urlopen", return_value=openai_response()) as transport:
            result = draft(CONVERSATION, CHECKLIST)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(len(result["answers"]), 8)
        self.assertEqual(result["answers"]["c04"]["answer"], 20)
        self.assertEqual(result["answers"]["c01"]["checked_turn_range"]["start_idx"], 1)
        payload = result["request"]
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["response_format"]["json_schema"]["schema"]["required"], ["answers"])
        self.assertIn("#1", payload["messages"][1]["content"])
        sent = transport.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertNotIn("unused-test-key", json.dumps(result))

    def test_dynamic_checklist_is_in_prompt(self):
        changed = {**CHECKLIST, "criteria": [dict(CHECKLIST["criteria"][0], id="custom")],
                   "global_rules": ["New rule"]}
        request = build_request(CONVERSATION, changed, "gpt-4o-mini")
        self.assertIn("custom", request["messages"][1]["content"])
        self.assertIn("New rule", request["messages"][1]["content"])
        self.assertEqual(request["response_format"]["json_schema"]["schema"]["properties"]
                         ["answers"]["items"]["properties"]["criterion_id"]["enum"], ["custom"])
        decimal = {**CHECKLIST, "criteria": [dict(CHECKLIST["criteria"][3], id="decimal",
                                                  scale=[{"value": 0, "text": "none"},
                                                         {"value": 2.5, "text": "partial"}])]}
        answers = model_answers(decimal)
        with patch("provider.urlopen", return_value=openai_response(answers)):
            self.assertEqual(draft(CONVERSATION, decimal)["answers"]["decimal"]["answer"], 2.5)

    def test_invalid_json_refusal_and_incomplete_are_errors(self):
        cases = [Response({"choices": [{"finish_reason": "stop", "message": {"content": "{bad"}}]}),
                 openai_response(refusal="Cannot help"),
                 Response({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})]
        for response in cases:
            with self.subTest(response=response.value), patch("provider.urlopen", return_value=response):
                with self.assertRaises(ProviderFailure):
                    draft(CONVERSATION, CHECKLIST)

    def test_missing_duplicate_criterion_invalid_na_and_bad_turn(self):
        variants = []
        variants.append(model_answers()[:-1])
        duplicate = model_answers()
        duplicate[-1]["criterion_id"] = "c01"
        variants.append(duplicate)
        na = model_answers()
        na[0]["decision"] = "na"
        na[0]["na_reason"] = "Not applicable"
        variants.append(na)
        turn = model_answers()
        turn[0]["evidence_turn_indices"] = [999]
        variants.append(turn)
        checked = model_answers()
        checked[0]["checked_turn_range"]["end_idx"] = 999
        variants.append(checked)
        for answers in variants:
            with self.subTest(answers=answers), patch("provider.urlopen", return_value=openai_response(answers)):
                with self.assertRaises(ProviderFailure):
                    draft(CONVERSATION, CHECKLIST)

    def test_401_no_retry_429_500_retry_once_and_timeout(self):
        def http_error(code):
            return HTTPError("https://api.openai.com", code, "test", None, None)
        with patch("provider.urlopen", side_effect=http_error(401)) as transport:
            with self.assertRaisesRegex(ProviderFailure, "401"):
                draft(CONVERSATION, CHECKLIST)
            self.assertEqual(transport.call_count, 1)
        for code in (429, 500):
            with self.subTest(code=code), patch("provider.sleep"), patch(
                    "provider.urlopen", side_effect=[http_error(code), openai_response()]) as transport:
                self.assertEqual(draft(CONVERSATION, CHECKLIST)["provider"], "openai")
                self.assertEqual(transport.call_count, 2)
        with patch("provider.sleep"), patch("provider.urlopen", side_effect=TimeoutError) as transport:
            with self.assertRaisesRegex(ProviderFailure, "connection failed"):
                draft(CONVERSATION, CHECKLIST)
            self.assertEqual(transport.call_count, 2)

    def test_gemini_adapter_still_parses_legacy_shape(self):
        legacy = [{"criterion_id": c["id"], "applicability": "applicable", "na_reason": "",
                   "answer": max(s["value"] for s in c["scale"]) if c["type"] == "scale" else
                   "no" if c["type"] == "flag" else "yes", "evidence_idx": [1],
                   "checked_turn_range": {"start_idx": 1, "end_idx": len(CONVERSATION["transcript"])},
                   "explanation": "Checked", "violation_type": ""} for c in CHECKLIST["criteria"]]
        raw = Response({"candidates": [{"content": {"parts": [{"text": json.dumps({"answers": legacy})}]}}]})
        with patch.dict(os.environ, {"CQA_PROVIDER": "gemini", "GEMINI_API_KEY": "unused-test-key"}), \
             patch("provider.urlopen", return_value=raw) as transport:
            result = draft(CONVERSATION, CHECKLIST)
        self.assertEqual(result["provider"], "gemini")
        self.assertIn("models/gpt-4o-mini:generateContent", transport.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
