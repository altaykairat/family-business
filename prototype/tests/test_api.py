import json
import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))

try:
    from fastapi.testclient import TestClient
    import server
    from compare import compare
    from provider import ProviderFailure
    from store import Store
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "Install prototype/requirements-dev.txt")
class ApiTests(unittest.TestCase):
    def test_provider_failure_is_visible_in_trace(self):
        with TemporaryDirectory() as temp:
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            client = TestClient(server.app)
            client.post("/api/import-dev")
            eid = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001"}).json()["id"]
            trace = {"provider": "openai", "model": "test", "prompt_version": "test",
                     "request": {"model": "test"}, "raw": "{broken", "usage": None,
                     "duration_seconds": .1, "error": "invalid JSON"}
            with patch("server.draft", side_effect=ProviderFailure("invalid JSON", trace)):
                result = client.post(f"/api/evaluations/{eid}/ai").json()
            self.assertEqual(result["last_provider_error"]["raw"], "{broken")
            self.assertEqual(result["events"][-1]["kind"], "ai_error")
            self.assertTrue(client.get(f"/api/evaluations/{eid}/prompt").json()["sent"])

    def test_checklist_change_reaches_prompt_without_code_change(self):
        with TemporaryDirectory() as temp:
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            client = TestClient(server.app)
            for path in ("/", "/app.js", "/style.css"):
                self.assertEqual(client.get(path).status_code, 200)
            client.post("/api/import-dev")
            old = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001"}).json()
            checklist = client.get("/api/checklists/default").json()
            checklist["criteria"][1]["title"] = "Проверка нового критерия"
            saved = client.post("/api/checklists", json=checklist).json()
            self.assertEqual(saved["version"], 2)
            new = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001",
                                                        "checklist_version": 2}).json()
            new_prompt = client.get(f"/api/evaluations/{new['id']}/prompt").json()["request"]["messages"][1]["content"]
            old_prompt = client.get(f"/api/evaluations/{old['id']}/prompt").json()["request"]["messages"][1]["content"]
            self.assertIn("Проверка нового критерия", new_prompt)
            self.assertNotIn("Проверка нового критерия", old_prompt)

    def test_mock_draft_and_batch_are_labeled(self):
        with TemporaryDirectory() as temp:
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            client = TestClient(server.app)
            client.post("/api/import-dev")
            with patch.dict(os.environ, {"CQA_PROVIDER": "mock", "CQA_LLM_MODEL": "unused-real-name"}):
                eid = client.post("/api/evaluations", json={"conversation_id": "p1-kz-001"}).json()["id"]
                result = client.post(f"/api/evaluations/{eid}/ai").json()
                self.assertEqual(result["source"], "mock-technical-demo")
                self.assertEqual(result["score"]["total"], 0)
                self.assertEqual(result["events"][-1]["detail"]["provider"], "mock")
                metrics = compare([{"conversation_id": "p1-kz-001", "answers": result["answers"]}], server.store)
                self.assertIsNone(metrics["exact_agreement"])
                self.assertIn("mock/manual excluded", metrics["missing_or_incomplete"][0])
                job = client.post("/api/batch", json={"conversation_ids": ["p1-ru-001", "p1-en-001"]}).json()
                deadline = time.monotonic() + 3
                while job["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    job = client.get("/api/batch/" + job["id"]).json()
                self.assertEqual(job["status"], "complete")
                self.assertEqual([item["status"] for item in job["results"]], ["valid", "valid"])

    def test_real_dev_import_manual_review_and_appeal(self):
        with TemporaryDirectory() as temp:
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            client = TestClient(server.app)
            self.assertEqual(client.post("/api/import-dev").json()["imported"], 65)
            conversation = client.get("/api/conversations/p1-ru-001").json()["conversation"]
            self.assertEqual(conversation["language"], "RU")
            created = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001"}).json()
            self.assertIsNone(created["score"])
            eid = created["id"]
            self.assertIn("#10", client.get(f"/api/evaluations/{eid}/prompt").json()["request"]["messages"][1]["content"])
            for criterion in created["checklist_snapshot"]["criteria"]:
                answer = {"applicability": "applicable", "answer":
                          max(step["value"] for step in criterion["scale"]) if criterion["type"] == "scale" else
                          "no" if criterion["type"] == "flag" else "yes",
                          "evidence_idx": [10], "explanation": "Проверена реплика оператора."}
                response = client.patch(f"/api/evaluations/{eid}/answers/{criterion['id']}", json=answer)
                self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["score"]["total"], 100)
            opened = client.post(f"/api/evaluations/{eid}/appeals",
                                 json={"criterion_id": "c04", "comment": "Есть пропуск"}).json()
            aid = opened["appeals"][0]["id"]
            decided = client.post(f"/api/appeals/{aid}/decision", json={
                "action": "change", "reason": "Существенный вопрос открыт",
                "answer": {"answer": 10, "explanation": "Указан только частичный ответ."}})
            self.assertEqual(decided.status_code, 200)
            self.assertEqual(decided.json()["score"]["total"], 90)
            self.assertEqual(decided.json()["appeals"][0]["status"], "decided")


if __name__ == "__main__":
    unittest.main()
