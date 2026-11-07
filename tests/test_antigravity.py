"""Unit and integration tests for Antigravity Fine-Grained Decomposition prototypes."""

import shutil
import unittest
from pathlib import Path

import cqa.checklist as ck
import cqa.conversation as cv
import cqa.scoring as scoring
from cqa.common import RUNS, load_json

from variants.antigravity.dossier import (
    build_fact_extraction_prompt,
    render_dossier_markdown,
    validate_dossier,
)
from variants.antigravity.prompts import build_prompts
from variants.antigravity.runner import ingest, prepare, report, simulate
from variants.antigravity.subquestions import derive_subquestions, reduce_subquestions


class TestSubquestions(unittest.TestCase):
    def setUp(self):
        self.doc = ck.load("default@1")
        self.crit = {c["id"]: c for c in ck.active_criteria(self.doc)}

    def test_c01_subquestions(self):
        c01 = self.crit["c01"]
        sqs = derive_subquestions(c01)
        ids = [s["id"] for s in sqs]
        self.assertIn("c01_greet", ids)
        self.assertIn("c01_lang", ids)
        self.assertIn("c01_intro", ids)
        self.assertIn("c01_first_turn", ids)

    def test_c03_na_gate(self):
        c03 = self.crit["c03"]
        sqs = derive_subquestions(c03)
        self.assertTrue(any(s["kind"] == "na_gate" for s in sqs))

    def test_dynamic_fallback_for_new_criterion(self):
        # Rule: no hardcoded criteria! Ensure a brand new c09 works dynamically.
        new_c = {
            "id": "c09",
            "section": "conduct",
            "title": "Опрос качества",
            "description": "Да — оператор предложил клиенту оценить консультацию в конце разговора.",
            "type": "binary",
            "weight": 5,
            "na_allowed": True,
            "na_condition": "Чат завершился до предоставления ответа.",
        }
        sqs = derive_subquestions(new_c)
        self.assertTrue(len(sqs) >= 2)
        self.assertEqual(sqs[0]["kind"], "na_gate")
        self.assertTrue(sqs[0]["id"].startswith("c09_na"))

    def test_reduction_binary_success(self):
        c01 = self.crit["c01"]
        sq_answers = {
            "c01_greet": True,
            "c01_lang": True,
            "c01_intro": True,
            "c01_first_turn": True,
        }
        ans, meta = reduce_subquestions(c01, sq_answers)
        self.assertEqual(ans, "yes")

    def test_reduction_binary_failure(self):
        c01 = self.crit["c01"]
        sq_answers = {
            "c01_greet": True,
            "c01_lang": True,
            "c01_intro": False,  # Did not introduce name
            "c01_first_turn": True,
        }
        ans, meta = reduce_subquestions(c01, sq_answers)
        self.assertEqual(ans, "no")
        self.assertIn("c01_intro", meta["failed_clauses"])

    def test_reduction_na_gate(self):
        c02 = self.crit["c02"]
        sq_answers = {
            "c02_na": True,  # Data was sufficient from start -> N/A
            "c02_known_details": True,
            "c02_clarify_missing": False,
        }
        ans, meta = reduce_subquestions(c02, sq_answers)
        self.assertEqual(ans, "na")

    def test_reduction_flag_gross_error(self):
        c08 = self.crit["c08"]
        sq_answers = {
            "c08_insult": False,
            "c08_bypass": True,  # Off-platform deal offered!
            "c08_leak": False,
        }
        ans, meta = reduce_subquestions(c08, sq_answers)
        self.assertEqual(ans, "yes")  # Flag raised!

    def test_reduction_scale_ladder(self):
        c04 = self.crit["c04"]
        sq_answers = {
            "c04_na": False,
            "c04_step_20": False,
            "c04_step_15": True,  # Main question resolved, minor detail missed
            "c04_step_10": True,
        }
        ans, meta = reduce_subquestions(c04, sq_answers)
        self.assertEqual(ans, 15)

    def test_criterion_replacement_does_not_break_variant(self):
        """P30 requirement: Replacing a criterion with a custom one does not break reduction."""
        replaced_c07 = {
            "id": "c07_speed",
            "section": "conduct",
            "title": "Скорость реакции",
            "type": "binary",
            "weight": 5,
            "sub_questions": [
                {
                    "id": "c07_speed_first",
                    "kind": "clause",
                    "question": "Первый ответ дан быстрее 60 секунд?",
                    "required": True,
                },
                {
                    "id": "c07_speed_pauses",
                    "kind": "clause",
                    "question": "Все последующие паузы не превышали 45 секунд?",
                    "required": True,
                }
            ],
            "reduction_rule": {
                "op": "all",
                "true_val": "yes",
                "false_val": "no"
            }
        }
        # Subquestions derived directly from data
        sqs = derive_subquestions(replaced_c07)
        self.assertEqual(len(sqs), 2)
        self.assertEqual(sqs[0]["id"], "c07_speed_first")

        # Reduction based purely on declarative reduction_rule
        res_ok, meta_ok = reduce_subquestions(replaced_c07, {"c07_speed_first": True, "c07_speed_pauses": True})
        self.assertEqual(res_ok, "yes")

        res_fail, meta_fail = reduce_subquestions(replaced_c07, {"c07_speed_first": True, "c07_speed_pauses": False})
        self.assertEqual(res_fail, "no")

    def test_criterion_without_subquestions_v1_passthrough(self):
        """P30 requirement: Criterion without subquestions evaluates directly as in V1."""
        c_v1 = {
            "id": "c99_generic",
            "section": "conduct",
            "title": "Общий критерий без подвопросов",
            "type": "binary",
            "weight": 10,
        }
        # Direct answer pass-through
        res, meta = reduce_subquestions(c_v1, {"c99_generic": "yes"})
        self.assertEqual(res, "yes")
        self.assertEqual(meta.get("mode"), "v1_passthrough")


class TestDossier(unittest.TestCase):
    def setUp(self):
        self.doc = ck.load("default@1")
        self.conv = cv.load("DEV", "p1-ru-001")
        self.facts = cv.facts(self.conv, 30)

    def test_build_fact_extraction_prompt(self):
        prompt = build_fact_extraction_prompt(self.doc, self.conv, self.facts)
        self.assertIn("Fact Extraction Dossier", prompt)
        self.assertIn("p1-ru-001", prompt)
        self.assertIn("language_audit", prompt)

    def test_validate_dossier_good(self):
        data = {
            "language_audit": {"evidence_idx": 1},
            "contact_audit": {"first_operator_turn_idx": 10},
        }
        valid_data, issues = validate_dossier(self.conv, data)
        self.assertEqual(len(issues), 0)

    def test_validate_dossier_bad_turn(self):
        data = {
            "language_audit": {"evidence_idx": 999},  # Turn 999 does not exist
        }
        valid_data, issues = validate_dossier(self.conv, data)
        self.assertTrue(any("999" in i for i in issues))

    def test_render_dossier_markdown(self):
        data = {
            "language_audit": {"client_language": "RU", "operator_language_first_turn": "RU", "languages_match": True},
            "contact_audit": {"first_operator_turn_idx": 10, "greeting_present": True},
        }
        md = render_dossier_markdown(data)
        self.assertIn("Объективное досье фактов диалога", md)
        self.assertIn("RU", md)


class TestPromptsBuilder(unittest.TestCase):
    def setUp(self):
        self.doc = ck.load("default@1")
        self.conv = cv.load("DEV", "p1-ru-001")
        self.facts = cv.facts(self.conv, 30)

    def test_build_v3_focus(self):
        prompts = build_prompts("v3_focus", self.doc, self.conv, self.facts)
        self.assertEqual(len(prompts), 8)
        self.assertEqual(prompts[0]["part"], "c01")
        self.assertIn("c01 · Установление контакта", prompts[0]["text"])

    def test_build_v4_mono(self):
        prompts = build_prompts("v4_mono", self.doc, self.conv, self.facts, dossier={"test": 1})
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["part"], "all")
        self.assertIn("Досье фактов диалога", prompts[0]["text"])

    def test_build_v_subq_bundled(self):
        prompts = build_prompts("v_subq_bundled", self.doc, self.conv, self.facts)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["part"], "all")
        self.assertIn("c01_greet", prompts[0]["text"])


class TestPipelineE2E(unittest.TestCase):
    def setUp(self):
        self.test_run_id = "test_run_anti_e2e"
        self.run_dir = RUNS / self.test_run_id
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)

    def tearDown(self):
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)

    def test_v3_focus_e2e_cycle(self):
        # 1. Prepare
        out_dir = prepare(
            run_id=self.test_run_id,
            dataset="DEV",
            checklist_ref="default@1",
            variant="v3_focus",
            ids=["p1-ru-001", "p1-ru-012"],
        )
        self.assertTrue(out_dir.exists())
        manifest = load_json(out_dir / "manifest.json")
        self.assertEqual(manifest["variant"], "v3_focus")
        self.assertIn("token_projection", manifest)

        # 2. Simulate responses
        count = simulate(self.test_run_id, agent="antigravity")
        self.assertEqual(count, 16)  # 2 dialogs * 8 criteria

        # 3. Ingest
        summary = ingest(self.test_run_id, agents=["antigravity"])
        self.assertEqual(summary["antigravity"]["done"], 2)

        # 4. Check validated file compatibility
        val_file = self.run_dir / "validated" / "antigravity" / "p1-ru-001.json"
        self.assertTrue(val_file.exists())
        val_doc = load_json(val_file)
        self.assertIn("score", val_doc)
        self.assertIn("total", val_doc["score"])
        self.assertIn("rows", val_doc["score"])
        self.assertEqual(len(val_doc["answers"]), 8)

        # 5. Report
        rep_res = report(self.test_run_id)
        rep_file = Path(rep_res["report_file"])
        self.assertTrue(rep_file.exists())
        content = rep_file.read_text(encoding="utf-8")
        self.assertIn("Экономика токенов", content)
        self.assertIn("Gemini 1.5 Flash", content)

    def test_v_subq_bundled_e2e_cycle(self):
        bundled_run = "test_run_anti_bundled"
        b_dir = RUNS / bundled_run
        if b_dir.exists():
            shutil.rmtree(b_dir)

        try:
            prepare(
                run_id=bundled_run,
                dataset="DEV",
                checklist_ref="default@1",
                variant="v_subq_bundled",
                ids=["p1-ru-001"],
            )
            count = simulate(bundled_run, agent="antigravity")
            self.assertEqual(count, 1)  # 1 bundled file

            summary = ingest(bundled_run, agents=["antigravity"])
            self.assertEqual(summary["antigravity"]["done"], 1)

            val_file = b_dir / "validated" / "antigravity" / "p1-ru-001.json"
            self.assertTrue(val_file.exists())
            val_doc = load_json(val_file)
            self.assertEqual(val_doc["score"]["total"], 100.0)
        finally:
            if b_dir.exists():
                shutil.rmtree(b_dir)


if __name__ == "__main__":
    unittest.main()
