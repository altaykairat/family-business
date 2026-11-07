"""Sprint 1 features in a temporary workspace. Model answers are hand-written fixtures, not LLM output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cqa import api, common, manual, run, state

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


if __name__ == "__main__":
    unittest.main()
