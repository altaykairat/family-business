import unittest

from cqa import checklist as ck
from cqa import conversation as cv
from cqa import responses as rs
from cqa.scoring import score

DOC = ck.load("default@1")
ALL_YES = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}


class ScoringTest(unittest.TestCase):
    def test_all_full_is_100(self):
        s = score(DOC, ALL_YES)
        self.assertEqual(s["total"], 100.0)
        self.assertFalse(s["gross_error"])
        self.assertTrue(s["complete"])

    def test_na_redistribution(self):
        # c02 (15) and c03 (20) N/A → applicable weight 65; c07 No loses 5/65*100
        a = {**ALL_YES, "c02": "na", "c03": "na", "c07": "no"}
        s = score(DOC, a)
        self.assertEqual(s["applicable_weight"], 65.0)
        self.assertEqual(s["total"], 92.31)  # 60/65*100 = 92.307… → 92.31
        row = next(r for r in s["rows"] if r["criterion_id"] == "c01")
        self.assertEqual(row["effective_weight"], 15.38)

    def test_scale_proportional(self):
        s = score(DOC, {**ALL_YES, "c04": 15, "c06": 7})
        # 20*15/20=15, 15*7/10=10.5 → 100-5-4.5
        self.assertEqual(s["total"], 90.5)

    def test_round_half_up(self):
        # c04 = 5 → 5 points; c06 = 4 → 6 points; c02,c03 N/A → (10+5+15+6+5)/65*100 = 63.0769…
        s = score(DOC, {**ALL_YES, "c02": "na", "c03": "na", "c04": 5, "c06": 4})
        self.assertEqual(s["total"], 63.08)

    def test_gross_error_flag_does_not_change_points(self):
        s = score(DOC, {**ALL_YES, "c08": "yes"})
        self.assertEqual(s["total"], 100.0)
        self.assertTrue(s["gross_error"])
        self.assertEqual(s["gross_error_criteria"], ["c08"])

    def test_no_applicable_criteria(self):
        a = {"c01": None, "c02": "na", "c03": "na", "c04": "na", "c08": "no"}
        s = score(DOC, a)
        self.assertIsNone(s["total"])
        self.assertFalse(s["complete"])

    def test_sections(self):
        s = score(DOC, {**ALL_YES, "c01": "no"})
        start = next(x for x in s["sections"] if x["section_id"] == "start")
        self.assertEqual(start["score_pct"], 60.0)  # 15 of 25


class ResponsesTest(unittest.TestCase):
    def setUp(self):
        self.conv = cv.load("dev", "p1-ru-012")

    def test_parse_fenced_json_with_trailing_comma(self):
        data, err = rs.parse('Вот ответ:\n```json\n{"criteria": [{"criterion_id": "c01",}],}\n```')
        self.assertIsNone(err)
        self.assertEqual(data["criteria"][0]["criterion_id"], "c01")

    def test_normalize_answers(self):
        c07 = next(c for c in DOC["criteria"] if c["id"] == "c07")
        c01 = next(c for c in DOC["criteria"] if c["id"] == "c01")
        c04 = next(c for c in DOC["criteria"] if c["id"] == "c04")
        self.assertEqual(rs.normalize_answer("Да", c07), ("yes", None))
        self.assertEqual(rs.normalize_answer("N/A", c07)[0], "na")
        self.assertIsNone(rs.normalize_answer("na", c01)[0])  # N/A not allowed for c01
        self.assertEqual(rs.normalize_answer(17, c04)[0], 15)  # snapped to nearest step

    def test_quote_and_idx_checks(self):
        data = {"criteria": [
            {"criterion_id": "c01", "answer": "yes", "evidence": [{"idx": 10, "quote": "Здравствуйте. Я Илья."}]},
            {"criterion_id": "c07", "answer": "no", "evidence": [{"idx": 99, "quote": "нет такой"}]},
        ]}
        v = rs.validate(self.conv, DOC, data, 0.9, ["c01", "c07"])
        self.assertEqual(v["answers"], {"c01": "yes", "c07": "no"})
        self.assertTrue(v["criteria"]["c01"]["evidence_checks"][0]["quote_ok"])
        self.assertTrue(any("#99" in i["msg"] for i in v["issues"]))
        self.assertEqual(v["status"], "error")


class FactsTest(unittest.TestCase):
    def test_facts_p1_ru_012(self):
        f = cv.facts(cv.load("dev", "p1-ru-012"))
        self.assertEqual(f["escalation_idx"], 9)
        self.assertEqual(f["first_operator_turn_idx"], 10)
        self.assertEqual(f["last_turn"]["speaker"], "system")
        self.assertEqual(f["seconds_from_last_client_to_end"], 18)

    def test_operator_name_guess(self):
        self.assertEqual(cv.guess_operator_name(cv.load("dev", "p1-ru-001")), "Алина")
        self.assertEqual(cv.guess_operator_name(cv.load("dev", "p1-kz-002")), "Нұрлан")
        self.assertEqual(cv.guess_operator_name(cv.load("dev", "p1-en-009")), "Leo")


if __name__ == "__main__":
    unittest.main()
