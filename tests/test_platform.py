"""End-to-end tests of the platform in a temporary workspace (the real history/ and runs/ are never touched).

Model answers here are hand-written test fixtures for the parsing / scoring / appeal machinery — not LLM output.
"""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cqa import analytics, api, appeals, checklist as ck, common, db, events, evaluations, groundtruth
from cqa import loop, metrics, people, providers, run, state, tasks

IDS = ["p1-ru-001", "p1-ru-012", "p1-en-009"]
BASE = {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}
# agent answers per conversation: a1 and a2 agree except on c07 of p1-ru-012
ANSWERS = {
    "a1": {"p1-ru-001": BASE, "p1-ru-012": {**BASE, "c07": "no"}, "p1-en-009": {**BASE, "c04": 15}},
    "a2": {"p1-ru-001": BASE, "p1-ru-012": {**BASE, "c07": "no"}, "p1-en-009": {**BASE, "c04": 15}},
    "a3": {"p1-ru-001": {**BASE, "c02": "yes"}, "p1-ru-012": {**BASE, "c07": "na"}, "p1-en-009": BASE},
}


def response(conv_id, answers):
    return "```json\n" + json.dumps({"criteria": [
        {"criterion_id": k, "answer": v, "evidence": [{"idx": 10, "quote": ""}], "explanation": f"{k} test"}
        for k, v in answers.items()]}, ensure_ascii=False) + "\n```"


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cqa-test-"))
        shutil.copytree(common.ROOT / "data" / "checklists", cls.tmp / "data" / "checklists")
        common.use_workspace(cls.tmp)
        state._CACHE.clear()
        cfg = common.load_config()
        cfg["agents"] = ["a1", "a2", "a3"]
        run.prepare("t1", "dev", "default", "v1", cfg, ids=IDS, author="a-01")
        for agent, per in ANSWERS.items():
            for cid, ans in per.items():
                run.submit_response("t1", agent, cid, response(cid, ans), actor="a-01")
        run.ingest("t1", cfg)
        evaluations.aggregate("t1")
        people.seed_demo("dev", "a-01")
        people.confirm_suggestions("a-01")
        for a in ("a1", "a2", "a3", "majority"):
            evaluations.import_source(f"t1:{a}", "a-01")
        evaluations.publish_source("t1:majority", "a-01")

    @classmethod
    def tearDownClass(cls):
        common.use_workspace(None)
        state._CACHE.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def eid(self, agent, conv):
        return state.load().latest_by_key[("t1", agent, conv)]

    # --- evaluations -------------------------------------------------------------------
    def test_import_is_idempotent_and_keeps_versions(self):
        res = evaluations.import_source("t1:a1", "a-01")
        self.assertEqual(res["imported"], 0)
        ev = state.load().evaluations[self.eid("a1", "p1-ru-001")]
        self.assertEqual(ev["versions"]["checklist"]["ref"], "default@v1")
        self.assertIn("template", ev["versions"])
        self.assertEqual(ev["versions"]["checklist"]["criteria_revs"]["c07"], "c07@r1")

    def test_majority_and_review_flag(self):
        maj = common.load_json(common.RUNS / "t1" / "validated" / "majority" / "p1-ru-012.json")
        self.assertEqual(maj["answers"]["c07"], "no")
        self.assertAlmostEqual(maj["agreement"]["c07"]["share"], 2 / 3)
        self.assertEqual(maj["answers"]["c04"], 20)

    def test_card(self):
        card = evaluations.card("p1-ru-012")
        self.assertEqual(card["evaluation"]["agent"], "majority")
        c07 = next(c for c in card["evaluation"]["criteria"] if c["criterion_id"] == "c07")
        self.assertEqual(c07["original"]["answer"], "no")
        self.assertTrue(card["conversation"]["transcript"][0]["idx"] == 1)
        self.assertEqual(card["conversation"]["operator_id"], state.load().operator_of["p1-ru-012"])

    # --- appeals -------------------------------------------------------------------------
    def test_appeal_flow_type_rules_and_final(self):
        eid = self.eid("majority", "p1-ru-012")
        op = state.load().operator_of["p1-ru-012"]
        ap = appeals.file(eid, "c07", "клиент ушёл, я не успел попрощаться", op, turn_idx=14)["appeal_id"]
        with self.assertRaises(common.CQAError):  # the pipeline may never decide
            appeals.decide("orchestrator", "change", "x", "B", appeal_id=ap, new_answer="na")
        with self.assertRaises(common.CQAError):  # an operator may not decide either
            appeals.decide(op, "change", "x", "B", appeal_id=ap, new_answer="na")
        appeals.assign(ap, "r-01", "a-01")
        with self.assertRaises(common.CQAError):  # type C keeps the answer
            appeals.decide("r-01", "change", "x", "C", appeal_id=ap, new_answer="na")
        with self.assertRaises(common.CQAError):  # type A changes the answer
            appeals.decide("r-01", "confirm", "x", "A", appeal_id=ap)
        res = appeals.decide("r-01", "change", "связь прервалась через 18 с после реплики клиента", "B",
                             appeal_id=ap, new_answer="N/A")
        self.assertEqual(res["new_answer"], "na")
        fin = state.load().final(eid)
        self.assertEqual(fin["original"]["answers"]["c07"], "no")   # the original never changes
        self.assertEqual(fin["final"]["answers"]["c07"], "na")
        self.assertGreater(fin["final"]["total"], fin["original"]["total"])
        with self.assertRaises(common.CQAError):
            appeals.decide("r-01", "confirm", "again", "D", appeal_id=ap)
        appeals.mark_precedent(res["decision_id"], "r-01", "типичный обрыв связи")
        self.assertEqual(len(appeals.precedents("c07")), 1)
        text = appeals.render_precedents(appeals.precedents("c07"))
        self.assertIn("проверяющий: «na»", text)
        # regression: a1 still says "no" on the precedent → broken; majority-style candidate with na passes
        rg = loop.regression("t1:a1", "a-01", include_decisions=True)
        self.assertFalse(rg["passed"])
        rg3 = loop.regression("t1:a3", "a-01", include_decisions=True)
        self.assertTrue(rg3["passed"])

    def test_blind_mode_hides_model_answer(self):
        eid = self.eid("majority", "p1-en-009")
        ap = appeals.file(eid, "c04", "ответил на всё", "op-01")["appeal_id"]
        appeals.set_reviewer_settings("r-02", "r-02", blind=True)
        v = appeals.view(ap, "r-02")
        self.assertTrue(v["model_hidden"])
        self.assertIsNone(v["model"])
        v2 = appeals.view(ap, "r-02", reveal=True)
        self.assertEqual(v2["model"]["answer"], 15)
        appeals.set_reviewer_settings("r-02", "r-02", blind=False)

    def test_counterargument_task_recorded(self):
        eid = self.eid("majority", "p1-ru-001")
        ap = appeals.file(eid, "c02", "уточнил детали", "op-01")["appeal_id"]
        t = tasks.create("counterargument", "r-03", {"appeal_id": ap, "reviewer_reason": "данных хватало"})
        prompt = tasks.prompt(t["task_id"])["text"]
        self.assertIn("уточнил детали", prompt)
        self.assertNotIn("{{", prompt)
        tasks.submit(t["task_id"], '```json\n{"counterarguments": [{"claim": "c", "evidence_idx": [10]}], '
                                   '"summary": "s"}\n```', "r-03")
        tasks.view(t["task_id"], "r-03")
        d = appeals.decide("r-03", "confirm", "данных хватало сразу", "D", appeal_id=ap)
        self.assertTrue(state.load().decisions[d["decision_id"]]["saw_counterargument"])

    # --- versions, proposals, activation -------------------------------------------------------
    def test_proposal_accept_preview_activate(self):
        p = loop.create_proposal("a-01", "note", "Обрыв связи после «это всё» — N/A",
                                 [{"op": "add_note", "criterion_id": "c07", "value": "Если клиент завершил "
                                   "разговор и связь прервалась сразу после его реплики — N/A."}],
                                 rationale="тип B")
        self.assertTrue(p["ops_valid"])
        bad = loop.create_proposal("orchestrator", "weight", "c07 вес 3",
                                   [{"op": "set_weight", "criterion_id": "c07", "value": 3}], source="orchestrator")
        self.assertTrue(bad["warnings"])  # weight change without type C backing is flagged
        with self.assertRaises(common.CQAError):
            loop.accept(p["proposal_id"], "r-01")  # only an admin applies
        res = loop.accept(p["proposal_id"], "a-01", "принято")
        ref = res["new_version"]
        cid, ver = ck.parse_ref(ref)
        self.assertEqual(ck.status_of(cid, ver), "draft")
        self.assertEqual(ck.resolve("default")[1], 1)  # «default» still means the active v1
        self.assertEqual(ck.criterion_revs(ck.load(ref))["c07"], "c07@r2")
        self.assertEqual(ck.criterion_revs(ck.load(ref))["c01"], "c01@r1")
        with self.assertRaises(common.CQAError):
            loop.activate(ref, "a-01", "")  # no passed regression yet
        loop.activate(ref, "a-01", "демо: регрессия на эталоне будет позже", force=True)
        self.assertEqual(ck.resolve("default")[1], ver)
        loop.activate("default@1", "a-01", "откат")
        self.assertEqual(ck.resolve("default")[1], 1)
        loop.reject(bad["proposal_id"], "a-01", "нет решений типа C")
        self.assertEqual(loop.proposal(bad["proposal_id"])["status"], "rejected")

    def test_preview_replace_criterion(self):
        doc = ck.load("default@1")
        new = ck.apply_ops(doc, [{"op": "replace_criterion", "criterion_id": "c05", "value": {
            "id": "c09", "section": "conduct", "title": "Персонализация", "description": "Да — обратился по имени.",
            "type": "binary", "weight": 15, "na_allowed": False, "na_condition": None}}])
        pv = loop.preview(doc, new)
        self.assertEqual(pv["diff"]["added"], ["c09"])
        self.assertEqual(pv["diff"]["archived"], ["c05"])
        self.assertEqual(pv["ground_truth_labels_missing"], ["c09"])
        self.assertEqual(pv["reevaluation_llm_calls"]["v3_per_criterion"], 3)
        self.assertNotIn("c05", [c["id"] for c in ck.active_criteria(new)])
        with self.assertRaises(common.CQAError):  # a replaced criterion needs a new id
            ck.apply_ops(doc, [{"op": "replace_criterion", "criterion_id": "c05", "value": {"id": "c05"}}])

    def test_simulate_weights_no_llm(self):
        sim = loop.simulate({"c04": 30}, source="t1:a1", use_final=False)
        self.assertEqual(sim["llm_calls"], 0)
        row = next(r for r in sim["rows"] if r["conversation_id"] == "p1-en-009")
        self.assertLess(row["new_total"], row["old_total"])  # c04=15 of 20 weighs more now

    # --- ground truth and metrics -----------------------------------------------------------------
    def test_ground_truth_and_metrics(self):
        d = groundtruth.draft(["t1:a1", "t1:a2", "t1:a3"], "test-gold", actor="a-01")
        self.assertGreater(d["needs_review"], 0)
        draft = common.load_json(common.WORKSPACE / d["path"])
        with self.assertRaises(common.CQAError):
            groundtruth.save_version("test-gold", draft, "a-01", "unresolved")
        for conv, item in draft["items"].items():
            for cid, lab in item["labels"].items():
                if lab["status"] == "needs_review":
                    lab["answer"], lab["status"] = ANSWERS["a1"][conv][cid], "human"
        draft["items"]["p1-ru-012"]["labels"]["c07"]["answer"] = "na"
        draft["items"]["p1-ru-012"]["labels"]["c07"]["evidence_idx"] = [10]
        v = groundtruth.save_version("test-gold", draft, "a-01", "проверено")
        self.assertEqual(v["labels"], 24)
        rep = metrics.compare("test-gold", ["t1:a1", "t1:a3", "t1:majority"], save=True)
        by = {r["source"]: r for r in rep["sources"]}
        # a1 differs from gold only on p1-ru-012/c07 (no vs na)
        self.assertEqual(by["t1:a1"]["micro_accuracy"]["k"], 23)
        self.assertEqual(by["t1:a1"]["no_na_confusion"]["gold_na_pred_no"], 1)
        self.assertEqual(by["t1:a1"]["criteria"]["c07"]["share"], round(2 / 3, 4))
        self.assertIsNotNone(by["t1:a1"]["totals"]["mae"])
        self.assertEqual(by["t1:a1"]["evidence"]["match_gold_window"]["share"], 1.0)
        self.assertTrue(rep["pairwise"])
        self.assertTrue((common.WORKSPACE / rep["paths"]["md"]).exists())
        agr = metrics.agreement(["t1:a1", "t1:a2"])
        self.assertEqual(agr["overall"]["share"], 1.0)
        split = groundtruth.load("test-gold")["split"]
        self.assertEqual(sorted(split["tune"] + split["test"]), sorted(IDS))

    def test_split_is_stratified_and_stable(self):
        ids = [f"p1-ru-{i:03d}" for i in range(1, 11)] + [f"p1-kz-{i:03d}" for i in range(1, 6)]
        a, b = groundtruth.make_split(ids, 0.4, 1), groundtruth.make_split(ids, 0.4, 1)
        self.assertEqual(a, b)
        self.assertEqual(sum(c.startswith("p1-kz") for c in a["test"]), 2)

    # --- history, projection, time machine ---------------------------------------------------------
    def test_hash_chain_detects_tampering(self):
        self.assertTrue(events.verify()["ok"])
        path = common.HISTORY
        original = path.read_text(encoding="utf-8")
        try:
            lines = original.splitlines()
            ev = json.loads(lines[2])
            ev["actor"] = "someone-else"
            lines[2] = json.dumps(ev, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertFalse(events.verify()["ok"])
        finally:
            path.write_text(original, encoding="utf-8")

    def test_time_machine(self):
        first = events.read()[0]["ts"]
        st = state.load(until=first)
        self.assertLessEqual(len(st.events), len(events.read()))
        self.assertEqual(len(state.load(until="2000-01-01T00:00:00+00:00").events), 0)

    def test_projection_and_analytics(self):
        rows = db.query("SELECT agent, COUNT(*) n FROM fact_evaluation WHERE current=1 GROUP BY agent")
        self.assertEqual({r["agent"]: r["n"] for r in rows}, {"a1": 3, "a2": 3, "a3": 3, "majority": 3})
        with self.assertRaises(common.CQAError):
            db.query("DELETE FROM feed")
        ops = analytics.operators()
        self.assertTrue(all("n" in o["appeal_rate"] for o in ops))
        self.assertTrue(all(isinstance(o["vs_team"], str) for o in ops))  # n below the comparison minimum
        analytics.reviewers()
        analytics.criteria()
        analytics.model_errors("criterion")
        analytics.pairs()
        self.assertIn("evaluations", analytics.overview())

    # --- providers and API ---------------------------------------------------------------------------
    def test_command_provider(self):
        stub = self.tmp / "stub.py"
        stub.write_text("import sys; sys.stdin.read(); print('{\"ok\": 1}')", encoding="utf-8")
        prov = providers.CommandProvider("stub", {"type": "command", "enabled": True,
                                                   "command": [sys.executable, str(stub)]})
        prompt = self.tmp / "p.md"
        prompt.write_text("hi", encoding="utf-8")
        res = prov.complete(prompt, self.tmp / "r.md")
        self.assertEqual(res["status"], "done")
        off = providers.CommandProvider("off", {"type": "command", "enabled": False, "command": ["x"]})
        with self.assertRaises(common.CQAError):
            off.complete(prompt, self.tmp / "r2.md")
        self.assertTrue(providers.get("manual").manual)

    def test_api_contract_and_http(self):
        from cqa import server

        names = {c["name"] for c in api.describe()}
        for mc_cmd in ("appeal.file", "appeal.decide", "precedent.mark", "loop.analyze", "proposal.accept",
                       "regression.run", "version.activate", "export", "people.assign", "run.prepare"):
            self.assertIn(mc_cmd, names)
        srv = server.make_server("127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/v1/"
        try:
            with urllib.request.urlopen(base + "commands") as r:
                self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
                self.assertTrue(json.load(r)["ok"])
            eid = self.eid("a2", "p1-ru-001")
            body = json.dumps({"evaluation_id": eid, "criterion_id": "c01", "comment": "представился"}).encode()
            req = urllib.request.Request(base + "appeal.file", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-CQA-Actor": "op-01"})
            with urllib.request.urlopen(req) as r:
                out = json.load(r)
            self.assertTrue(out["ok"] and out["events"])
            ap = out["data"]["appeal_id"]
            body = json.dumps({"appeal_id": ap, "action": "confirm", "reason": "x", "type": "D"}).encode()
            req = urllib.request.Request(base + "appeal.decide", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-CQA-Actor": "op-01"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 403)
            cm.exception.close()
            with urllib.request.urlopen(base + f"conversation.card?conversation_id=p1-ru-001&actor=r-01") as r:
                self.assertTrue(json.load(r)["data"]["evaluation"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "appeal.file?evaluation_id=x")  # state change via GET
            self.assertEqual(cm.exception.code, 405)
            cm.exception.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_checklist_change_via_api_creates_version(self):
        res = api.call("checklist.change", "a-01", {"ops": json.dumps([{"op": "set_weight", "criterion_id": "c07",
                                                                         "value": 4}]),
                                                     "reason": "тест веса", "save": "true"})
        self.assertTrue(res["data"]["preview"]["rescore_without_llm"])
        ref = res["data"]["saved"]["ref"]
        self.assertTrue(ck.path_for(*ck.parse_ref(ref)).exists())
        self.assertEqual(ck.load("default@1")["criteria"][6]["weight"], 5)  # the old version is untouched


if __name__ == "__main__":
    unittest.main()
