import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))
from provider import draft

CHECKLIST = json.loads((ROOT / "data/checklists/default.json").read_text(encoding="utf-8"))
CONVERSATION = json.loads((ROOT / "data/dev/conversations/p1-en-001.json").read_text(encoding="utf-8"))


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass
    def read(self):
        return json.dumps(self.value).encode("utf-8")


def model_text():
    return json.dumps({"answers": [{"criterion_id": c["id"], "applicability": "applicable",
                                    "na_reason": "", "answer": min(step["value"] for step in c["scale"])
                                    if c["type"] == "scale" else "no",
                                    "evidence_idx": [1], "explanation": "Test"}
                                   for c in CHECKLIST["criteria"]]})


class ProviderTests(unittest.TestCase):
    def test_openai_adapter_parses_and_records_request(self):
        raw = {"choices": [{"message": {"content": model_text()}}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        with patch.dict(os.environ, {"CQA_PROVIDER": "openai", "CQA_LLM_MODEL": "test-model",
                                  "CQA_LLM_API_KEY": "unused-test-key", "CQA_LLM_URL": ""}), \
             patch("provider.urlopen", return_value=Response(raw)) as transport:
            result = draft(CONVERSATION, CHECKLIST)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(len(result["answers"]), 8)
        self.assertIn("#1", result["request"]["messages"][1]["content"])
        self.assertEqual(transport.call_args.args[0].full_url, "https://api.openai.com/v1/chat/completions")

    def test_gemini_adapter_parses_parts(self):
        raw = {"candidates": [{"content": {"parts": [{"text": model_text()}]}}],
               "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 40}}
        with patch.dict(os.environ, {"CQA_PROVIDER": "gemini", "CQA_LLM_MODEL": "test-model",
                                  "CQA_LLM_API_KEY": "unused-test-key", "CQA_LLM_URL": ""}), \
             patch("provider.urlopen", return_value=Response(raw)) as transport:
            result = draft(CONVERSATION, CHECKLIST)
        self.assertEqual(result["provider"], "gemini")
        self.assertIn("contents", result["request"])
        self.assertIn("models/test-model:generateContent", transport.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
