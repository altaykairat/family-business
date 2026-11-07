import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))

from engine import Invalid, blank_answers, normalize_conversation, score, validate_answers, validate_checklist
from store import Store


CHECKLIST = json.loads((ROOT / "data/checklists/default.json").read_text(encoding="utf-8"))
CONVERSATION = normalize_conversation({"conversation_id": "sample", "channel": "agent chat", "language": "RU",
                                      "transcript": [{"speaker": "client", "timestamp": "00:00", "text": "Вопрос"},
                                                     {"speaker": "operator", "timestamp": "00:01", "text": "Ответ"}]})


def complete_answers():
    answers = blank_answers(CHECKLIST)
    for criterion in CHECKLIST["criteria"]:
        answers[criterion["id"]] = {"applicability": "applicable", "answer":
                                    max(step["value"] for step in criterion["scale"]) if criterion["type"] == "scale" else
                                    "no" if criterion["type"] == "flag" else "yes",
                                    "na_reason": "", "evidence_idx": [2], "explanation": "Проверена реплика оператора."}
    return answers


class EngineTests(unittest.TestCase):
    def test_default_rubric_and_perfect_score(self):
        validate_checklist(CHECKLIST)
        result = score(CHECKLIST, CONVERSATION, complete_answers())
        self.assertEqual(result["total"], 100)
        self.assertFalse(result["flags"]["c08"])

    def test_na_redistribution_and_partial_steps(self):
        answers = complete_answers()
        answers["c02"].update(applicability="na", answer=None, na_reason="Сведения были полными")
        answers["c04"]["answer"] = 15
        answers["c06"]["answer"] = 7
        result = score(CHECKLIST, CONVERSATION, answers)
        self.assertEqual(result["total"], 88.82)
        self.assertEqual(result["sections"]["safety"], 87.5)

    def test_na_forbidden_and_invalid_evidence(self):
        answers = complete_answers()
        answers["c01"].update(applicability="na", answer=None, evidence_idx=[999])
        errors = validate_answers(CHECKLIST, CONVERSATION, answers)
        self.assertTrue(any("N/A is forbidden" in error for error in errors))
        self.assertTrue(any("outside transcript" in error for error in errors))
        with self.assertRaises(Invalid):
            score(CHECKLIST, CONVERSATION, answers)

    def test_only_exact_scale_steps(self):
        answers = complete_answers()
        answers["c06"]["answer"] = 8
        self.assertTrue(any("scale step" in error for error in validate_answers(CHECKLIST, CONVERSATION, answers)))

    def test_flag_does_not_zero_total_and_empty_denominator(self):
        answers = complete_answers()
        answers["c08"]["answer"] = "yes"
        result = score(CHECKLIST, CONVERSATION, answers)
        self.assertEqual(result["total"], 100)
        self.assertTrue(result["flags"]["c08"])
        narrow = {**CHECKLIST, "criteria": [CHECKLIST["criteria"][1]]}
        answers = {"c02": {"applicability": "na", "answer": None, "na_reason": "Запрос полный",
                           "evidence_idx": [1], "explanation": "Уточнения не требовались"}}
        self.assertIsNone(score(narrow, CONVERSATION, answers)["total"])

    def test_all_no_multiple_na_and_manual_no_to_na(self):
        answers = complete_answers()
        for criterion in CHECKLIST["criteria"]:
            answers[criterion["id"]]["answer"] = 0 if criterion["type"] == "scale" else "no"
        self.assertEqual(score(CHECKLIST, CONVERSATION, answers)["total"], 0)
        answers["c01"]["answer"] = "yes"
        before = score(CHECKLIST, CONVERSATION, answers)["total"]
        answers["c02"].update(applicability="na", answer=None, na_reason="Details already sufficient")
        answers["c03"].update(applicability="na", answer=None, na_reason="No protected action")
        answers["c07"].update(applicability="na", answer=None, na_reason="Conversation ended early")
        after = score(CHECKLIST, CONVERSATION, answers)["total"]
        self.assertEqual(before, 10)
        self.assertEqual(after, 16.67)

    def test_checked_range_and_named_violation_for_new_answers(self):
        answers = complete_answers()
        answers["c01"]["checked_turn_range"] = {"start_idx": 2, "end_idx": 1}
        self.assertTrue(any("checked turn range" in e for e in
                            validate_answers(CHECKLIST, CONVERSATION, answers)))
        answers["c01"]["checked_turn_range"] = {"start_idx": 1, "end_idx": 1}
        self.assertTrue(any("outside checked turn range" in e for e in
                            validate_answers(CHECKLIST, CONVERSATION, answers)))
        answers["c01"]["checked_turn_range"] = {"start_idx": 1, "end_idx": 2}
        answers["c08"].update(answer="yes", violation_type="")
        self.assertTrue(any("violation must be named" in e for e in
                            validate_answers(CHECKLIST, CONVERSATION, answers)))
        answers["c08"]["violation_type"] = "Confidential information disclosed"
        self.assertEqual(score(CHECKLIST, CONVERSATION, answers)["total"], 100)

    def test_versioned_checklist_and_appeal_audit(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.db", ROOT / "data/checklists/default.json")
            store.import_conversations([CONVERSATION])
            initial = store.create_evaluation("sample")
            changed = dict(CHECKLIST)
            changed["title"] = "New version"
            self.assertEqual(store.save_checklist(changed)["version"], 2)
            self.assertEqual(initial["checklist_version"], 1)
            eid = initial["id"]
            for cid, answer in complete_answers().items():
                store.patch_answer(eid, cid, answer)
            store.appeal(eid, "c04", "Оценка завышена")
            appeal = store.evaluation(eid)["appeals"][0]
            result = store.decide_appeal(appeal["id"], {"action": "change", "reason": "Пропущен вопрос",
                                                        "answer": {"answer": 10}})
            self.assertEqual(score(result["checklist_snapshot"], CONVERSATION, result["answers"])["total"], 90)
            self.assertEqual(result["appeals"][0]["status"], "decided")
            self.assertTrue(any(event["kind"] == "appeal_decided" for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
