import json
import os
import re
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

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


class StubResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps(self.value).encode("utf-8")


def fixture_openai(request, **_kwargs):
    payload = json.loads(request.data)
    prompt = json.loads(payload["messages"][1]["content"])
    count = len(re.findall(r"(?m)^#\d+ \[", prompt["conversation"]))
    answers = [{"criterion_id": c["id"], "decision": "scored" if c["type"] == "scale" else
                "no" if c["type"] == "flag" else "yes",
                "scale_score": max(s["value"] for s in c["scale"]) if c["type"] == "scale" else None,
                "explanation": "Controlled transport fixture, not a real quality judgment.",
                "evidence_turn_indices": [1], "checked_turn_range": {"start_idx": 1, "end_idx": count},
                "na_reason": "", "violation_type": ""} for c in prompt["criteria"]]
    return StubResponse({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"answers": answers})}}], "usage": {
            "prompt_tokens": 100, "completion_tokens": 50}})


@unittest.skipIf(TestClient is None, "Install prototype/requirements-dev.txt")
class ApiTests(unittest.TestCase):
    def test_stubbed_openai_preserves_previous_result_after_failure(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
                "CQA_PROVIDER": "openai", "CQA_MODEL": "fixture-model",
                "OPENAI_API_KEY": "unused-test-key", "CQA_LLM_MAX_RETRIES": "0"}):
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            client = TestClient(server.app)
            client.post("/api/import-dev")
            eid = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001"}).json()["id"]
            with patch("provider.urlopen", side_effect=fixture_openai):
                good = client.post(f"/api/evaluations/{eid}/ai").json()
            self.assertIsNotNone(good["score"], good["last_provider_error"])
            self.assertEqual(good["score"]["total"], 100)
            self.assertEqual(good["answers"]["c01"]["checked_turn_range"]["start_idx"], 1)
            self.assertEqual(good["events"][-1]["detail"]["provider"], "openai")
            self.assertNotIn("unused-test-key", json.dumps(good))
            with patch("provider.urlopen", return_value=StubResponse({"choices": [{
                    "finish_reason": "stop", "message": {"content": "{invalid"}}]})):
                after = client.post(f"/api/evaluations/{eid}/ai").json()
            self.assertEqual(after["score"]["total"], 100)
            self.assertEqual(after["answers"], good["answers"])
            self.assertEqual(after["events"][-1]["kind"], "ai_error")
            patched = client.patch(f"/api/evaluations/{eid}/answers/c02", json={
                "applicability": "na", "answer": None, "na_reason": "All details were already provided",
                "evidence_idx": [1], "checked_turn_range": {"start_idx": 1, "end_idx": 2},
                "explanation": "No clarification was needed"}).json()
            self.assertEqual(patched["score"]["total"], 100)
            self.assertIn("c02", patched["changed_criteria"])
            appeal = client.post(f"/api/evaluations/{eid}/appeals", json={
                "criterion_id": "c04", "comment": "Fixture score is optimistic"}).json()
            aid = appeal["appeals"][0]["id"]
            reviewed = client.post(f"/api/appeals/{aid}/decision", json={
                "action": "change", "reason": "Manual review", "answer": {
                    "answer": 10, "explanation": "Only partial answer"}}).json()
            self.assertLess(reviewed["score"]["total"], 100)
            self.assertEqual(reviewed["appeals"][0]["status"], "decided")
            checklist = client.get("/api/checklists/default").json()
            checklist["criteria"][0]["title"] = "Revised contact criterion"
            new_version = client.post("/api/checklists", json=checklist).json()
            self.assertEqual(new_version["version"], 2)
            fresh = client.post("/api/evaluations", json={"conversation_id": "p1-ru-001",
                                                            "checklist_version": 2}).json()
            with patch("provider.urlopen", side_effect=fixture_openai):
                reassessed = client.post(f"/api/evaluations/{fresh['id']}/ai").json()
            self.assertEqual(reassessed["score"]["total"], 100)
            self.assertEqual(reassessed["checklist_version"], 2)
            self.assertEqual(reviewed["checklist_version"], 1)

    def test_retry_failed_record_with_no_prior_success(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
                "CQA_PROVIDER": "openai", "CQA_MODEL": "fixture-model",
                "OPENAI_API_KEY": "unused-test-key", "CQA_LLM_MAX_RETRIES": "0"}):
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            server.batch_jobs.clear()
            client = TestClient(server.app)
            client.post("/api/import-dev")
            with patch("provider.urlopen", side_effect=HTTPError("https://api.openai.com", 401,
                                                                    "test", None, None)):
                job = client.post("/api/batch", json={"conversation_ids": ["p1-en-001"]}).json()
                deadline = time.monotonic() + 5
                while job["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    job = client.get("/api/batch/" + job["id"]).json()
            self.assertEqual(job["results"][0]["status"], "error")
            with patch("provider.urlopen", side_effect=fixture_openai) as transport:
                retry = client.post("/api/batch/" + job["id"] + "/retry-failed").json()
                deadline = time.monotonic() + 5
                while retry["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    retry = client.get("/api/batch/" + retry["id"]).json()
            self.assertEqual(retry["results"][0]["status"], "valid")
            self.assertEqual(transport.call_count, 1)

    def test_future_group_import_uses_existing_batch(self):
        record = json.loads((ROOT / "data/dev/conversations/p1-kz-001.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
                "CQA_PROVIDER": "openai", "CQA_MODEL": "fixture-model",
                "OPENAI_API_KEY": "unused-test-key",
                "CQA_API_URL": "https://example.test/invoke?sig=unused-signed-value&group_id=DEV"}):
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            server.batch_jobs.clear()
            client = TestClient(server.app)
            with patch("server.urlopen", return_value=StubResponse({
                    "isSuccess": True, "data": {"records": [record]}})) as source:
                loaded = client.post("/api/import-group", json={"group_id": "CONTROL"}).json()
            self.assertEqual(loaded["imported"], 1)
            self.assertIn("group_id=CONTROL", source.call_args.args[0])
            with patch("provider.urlopen", side_effect=fixture_openai):
                job = client.post("/api/batch", json={"conversation_ids": [record["conversation_id"]]}).json()
                deadline = time.monotonic() + 5
                while job["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    job = client.get("/api/batch/" + job["id"]).json()
            self.assertEqual(job["results"][0]["status"], "valid")

    def test_batch_65_fixture_skip_and_retry_only_failed(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
                "CQA_PROVIDER": "openai", "CQA_MODEL": "fixture-model",
                "OPENAI_API_KEY": "unused-test-key", "CQA_LLM_MAX_RETRIES": "0",
                "CQA_BATCH_CONCURRENCY": "2"}):
            server.store = Store(Path(temp) / "api.db", ROOT / "data/checklists/default.json")
            server.batch_jobs.clear()
            client = TestClient(server.app)
            client.post("/api/import-dev")
            ids = [item["id"] for item in client.get("/api/bootstrap").json()["conversations"]]
            self.assertEqual(len(ids), 65)
            with patch("provider.urlopen", side_effect=fixture_openai) as transport:
                job = client.post("/api/batch", json={"conversation_ids": ids}).json()
                deadline = time.monotonic() + 25
                while job["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    job = client.get("/api/batch/" + job["id"]).json()
                self.assertEqual(job["status"], "complete")
                self.assertEqual(job["done"], 65)
                self.assertEqual(transport.call_count, 65)
                self.assertTrue(all(item["status"] == "valid" for item in job["results"]), job["results"][:3])
                self.assertEqual(job["metadata"]["checklist_version"], 1)
                repeat = client.post("/api/batch", json={"conversation_ids": ids}).json()
                self.assertEqual(repeat["done"], 65)
                self.assertTrue(all(item["status"] == "skipped" for item in repeat["results"]))
                self.assertEqual(transport.call_count, 65)

            failed_id = ids[0]
            with patch("provider.urlopen", side_effect=HTTPError("https://api.openai.com", 401,
                                                                    "test", None, None)):
                failed = client.post("/api/batch", json={"conversation_ids": [failed_id],
                                                          "force": True}).json()
                deadline = time.monotonic() + 5
                while failed["status"] != "complete" and time.monotonic() < deadline:
                    time.sleep(.05)
                    failed = client.get("/api/batch/" + failed["id"]).json()
            self.assertEqual(failed["results"][0]["status"], "error")
            with patch("provider.urlopen", side_effect=fixture_openai) as transport:
                retried = client.post("/api/batch/" + failed["id"] + "/retry-failed").json()
            self.assertEqual(retried["results"][0]["status"], "skipped")
            self.assertEqual(transport.call_count, 0)

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
                self.assertIsNone(result["score"])
                self.assertTrue(result["validation_errors"])
                self.assertTrue(all(answer["answer"] is None for answer in result["answers"].values()))
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
                self.assertEqual([item["status"] for item in job["results"]], ["needs_review", "needs_review"])

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
