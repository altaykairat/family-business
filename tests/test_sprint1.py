"""Sprint 1 features in a temporary workspace. Model answers are hand-written fixtures, not LLM output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cqa import api, checklist as ck, common, evaluations, manual, prompts, responses, run, state
from cqa import conversation as cv

IDS = ["p1-ru-001", "p1-ru-012"]
BASE = {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}


def response(answers):
    return "```json\n" + json.dumps({"criteria": [
        {"criterion_id": k, "answer": v, "evidence": [{"idx": 1, "quote": ""}], "explanation": "t"}
        for k, v in answers.items()]}, ensure_ascii=False) + "\n```"


class Workspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cqa-s1-"))
        shutil.copytree(common.ROOT / "data" / "checklists", cls.tmp / "data" / "checklists")
        common.use_workspace(cls.tmp)
        state._CACHE.clear()
        cfg = common.load_config()
        cfg["agents"] = ["a1", "a2"]
        run.prepare("p1", "dev", "default", "v1", cfg, ids=IDS, author="a-01")

    @classmethod
    def tearDownClass(cls):
        common.use_workspace(None)
        state._CACHE.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class PasteTest(Workspace):
    def test_next_save_status_cycle(self):
        st = manual.status("p1")
        self.assertEqual(st["agents"]["a1"], {"total": 2, "done": 0, "waiting": 2, "next": "p1-ru-001"})
        cur = manual.current("p1", "a1")
        self.assertEqual((cur["name"], cur["number"], cur["left"]), ("p1-ru-001", 1, 2))
        # the prompt itself is still in the clipboard → rejected, nothing saved
        res = manual.save("p1", cur["text"], "a1")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["saved"])
        # broken JSON → rejected
        self.assertFalse(manual.save("p1", "```json\n{\"criteria\": [\n```", "a1")["ok"])
        # a criterion is missing → validator error → rejected
        partial = {k: v for k, v in BASE.items() if k != "c08"}
        res = manual.save("p1", response(partial), "a1")
        self.assertFalse(res["ok"])
        self.assertTrue(any("c08" in e for e in res["errors"]))
        # valid answer → saved, queue moves on
        res = manual.save("p1", response(BASE), "a1")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["saved"].endswith("agents/a1/responses/p1-ru-001.md"))
        self.assertEqual(manual.current("p1", "a1")["name"], "p1-ru-012")
        # force keeps a rejected answer
        res = manual.save("p1", response(partial), "a1", force=True)
        self.assertTrue(res["saved"])
        self.assertIsNone(manual.current("p1", "a1")["name"])
        # agent choice: the first agent with work
        self.assertEqual(manual.current("p1")["agent"], "a2")
        # ingest reads what paste saved
        summary = run.ingest("p1", common.load_config(), agents=["a1"])
        self.assertEqual(summary["a1"]["done"], 2)

    def test_folder_without_manifest(self):
        box = self.tmp / "variants" / "x" / "runs" / "vr" / "agents" / "claude"
        (box / "prompts").mkdir(parents=True)
        (box / "prompts" / "b.md").write_text("prompt b", encoding="utf-8")
        (box / "prompts" / "a.md").write_text("prompt a", encoding="utf-8")
        path = str(box.parent.parent)
        self.assertEqual(manual.current(path)["name"], "a")
        res = manual.save(path, '{"anything": 1}')
        self.assertTrue(res["ok"])
        self.assertFalse(res["validated"])
        self.assertEqual(manual.status(path)["agents"]["claude"]["done"], 1)

    def test_api(self):
        r = api.call("paste.next", "a-01", {"run": "p1", "agent": "a2"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["name"], "p1-ru-001")
        with self.assertRaises(common.CQAError):  # operators cannot submit evaluations
            api.call("paste.save", "op-01", {"run": "p1", "agent": "a2", "text": response(BASE)})


class RulesInPromptTest(Workspace):
    def build(self, doc, template="v2", variant="v1", precedents=None):
        conv = cv.load("dev", "p1-ru-012")
        return prompts.build(template, doc, conv, cv.facts(conv, 30), variant, True, precedents)

    def test_empty_placeholders_vanish(self):
        b = self.build(ck.load("default@1"))[0]
        self.assertNotIn("{{", b["text"])
        self.assertNotIn("## Уточнения правил", b["text"])
        self.assertNotIn("## Прецеденты", b["text"])
        self.assertNotIn("\n\n\n", b["text"])
        self.assertNotIn("ctx:", b["prompt_id"])
        self.assertEqual(b["context"], {"rules": {}, "precedents": []})

    def test_clarifications_are_versioned_and_rendered(self):
        doc = ck.apply_ops(ck.load("default@1"), [
            {"op": "add_clarification", "value": {"criterion_id": "c07", "text": "Обрыв связи клиентом — N/A.",
                                                  "reason": "решения d-0001, d-0004"}},
            {"op": "add_clarification", "value": {"text": "Ответ бота оператору не засчитывается."}}])
        saved = ck.save_doc(doc, "default", "a-01", "test clarifications")
        doc2 = ck.load(saved["ref"])
        self.assertEqual(ck.clarification_revs(doc2), {"u1": "u1@r1", "u2": "u2@r1"})
        b = self.build(doc2)[0]
        self.assertIn("## Уточнения правил", b["text"])
        self.assertIn("1. **c07** — Обрыв связи клиентом — N/A. *Причина:* решения d-0001, d-0004", b["text"])
        self.assertIn("2. **Все критерии** — Ответ бота", b["text"])
        self.assertEqual(b["context"]["rules"], {"u1": "u1@r1", "u2": "u2@r1"})
        self.assertIn("|ctx:", b["prompt_id"])
        # per-criterion prompts: the c07 clarification is only in the c07 prompt, the global one everywhere
        parts = {p["part"]: p for p in self.build(doc2, variant="v3")}
        self.assertEqual(set(parts["c07"]["context"]["rules"]), {"u1", "u2"})
        self.assertEqual(set(parts["c01"]["context"]["rules"]), {"u2"})
        # template without {{RULES}}: clarifications are not lost
        v1 = self.build(doc2, template="v1")[0]["text"]
        self.assertIn("Уточнение (u1): Обрыв связи клиентом", v1)
        self.assertIn("Ответ бота оператору не засчитывается.", v1)
        # editing bumps the revision, archiving removes it from prompts, ids are never reused
        doc3 = ck.apply_ops(doc2, [{"op": "edit_clarification", "id": "u1", "value": {"text": "Обрыв — N/A, если ..."}},
                                   {"op": "archive_clarification", "id": "u2"},
                                   {"op": "add_clarification", "value": {"text": "третье"}}])
        saved3 = ck.save_doc(doc3, "default", "a-01", "edit")
        doc3 = ck.load(saved3["ref"])
        self.assertEqual(ck.clarification_revs(doc3), {"u1": "u1@r2", "u3": "u3@r1"})
        self.assertTrue(any("уточнение u1" in line for line in ck.diff(doc2, doc3)))
        with self.assertRaises(common.CQAError):
            ck.apply_ops(doc3, [{"op": "add_clarification", "value": {"criterion_id": "c99", "text": "x"}}])

    def test_precedents_section(self):
        prec = [{"decision_id": "d-0007", "criterion_id": "c07", "model_answer": "no", "reviewer_answer": "na",
                 "reason": "клиент перестал отвечать", "fragment": [{"idx": 3, "speaker": "client", "text": "алло"}]},
                {"decision_id": "d-0008", "criterion_id": "c04", "model_answer": 20, "reviewer_answer": 15,
                 "reason": "не уточнил детали", "fragment": []}]
        parts = {p["part"]: p for p in self.build(ck.load("default@1"), variant="v3", precedents=prec)}
        self.assertIn("## Прецеденты", parts["c07"]["text"])
        self.assertIn("#3 client: алло", parts["c07"]["text"])
        self.assertEqual(parts["c07"]["context"]["precedents"], ["d-0007"])
        self.assertNotIn("## Прецеденты", parts["c01"]["text"])
        self.assertNotEqual(parts["c07"]["prompt_id"], parts["c04"]["prompt_id"])


class InsufficientDataTest(Workspace):
    def test_validator_and_queue(self):
        conv = cv.load("dev", "p1-ru-001")
        doc = ck.load("default@1")
        data = json.loads(response(BASE).strip("`json\n"))
        data["criteria"][0]["insufficient_data"] = True
        data["criteria"][1]["insufficient_data"] = "false"
        data["criteria"][2]["insufficient_data"] = "maybe"
        data["criteria"][3]["confidence"] = 0.4
        v = responses.validate(conv, doc, data)
        self.assertEqual(v["insufficient_data"], ["c01"])
        self.assertFalse(v["criteria"]["c02"]["insufficient_data"])
        self.assertTrue(any("insufficient_data не bool" in i["msg"] for i in v["issues"]))
        sig = evaluations.review_signals({"criteria": v["criteria"]}, 0.7)
        self.assertIn("insufficient_data", sig["c01"])
        self.assertIn("confidence<0.7", sig["c04"])


if __name__ == "__main__":
    unittest.main()
