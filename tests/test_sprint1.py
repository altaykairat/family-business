"""Sprint 1 features in a temporary workspace. Model answers are hand-written fixtures, not LLM output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cqa import api, checklist as ck, common, evaluations, manual, prompts, responses, run, state, submission
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


class SubmissionTest(Workspace):
    def test_export_and_check(self):
        cfg = common.load_config()
        cfg["agents"] = ["e1"]
        run.prepare("ex", "dev", "default", "v1", cfg, ids=IDS, author="a-01")
        run.submit_response("ex", "e1", "p1-ru-001", response({**BASE, "c04": 15, "c06": 7}), actor="a-01")
        broken = {k: v for k, v in BASE.items() if k != "c05"}
        run.submit_response("ex", "e1", "p1-ru-012", response(broken), actor="a-01")
        run.ingest("ex", cfg)
        out = self.tmp / "results" / "dev" / "results.json"
        res = submission.export("dev", source="ex:e1", actor="a-01", path=str(out))
        self.assertTrue(res["check"]["ok"], res["check"])
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["results"]), 65)  # every DEV conversation, missing ones as errors
        rec = {r["conversation_id"]: r for r in doc["results"]}
        ok = rec["p1-ru-001"]
        self.assertEqual([c["criterion_id"] for c in ok["criteria"]],
                         ["contact", "discovery", "identification", "consultation", "empathy", "dialogue",
                          "closure", "critical"])
        vals = {c["criterion_id"]: c["value"] for c in ok["criteria"]}
        self.assertEqual(vals, {"contact": True, "discovery": None, "identification": None, "consultation": 15,
                                "empathy": True, "dialogue": 7, "closure": True, "critical": False})
        # weights 10/15/20/20/15/15/5, N/A on discovery and identification → 65 applicable
        self.assertEqual(ok["score"], {"final_score": round((10 + 15 + 15 + 7 / 10 * 15 + 5) / 65 * 100, 2),
                                       "critical_flag": False})
        self.assertEqual(ok["criteria"][0]["evidence_turns"], [1])
        self.assertEqual(rec["p1-ru-012"]["status"], "error")  # a missing criterion is an error, never a zero
        self.assertIn("c05", rec["p1-ru-012"]["error"])
        self.assertEqual(rec["p1-en-001"], {"conversation_id": "p1-en-001", "status": "error", "criteria": [],
                                            "score": None, "error": "Нет результата оценки для диалога."})

        # the check catches what the organizers forbid
        bad = json.loads(out.read_text(encoding="utf-8"))
        bad["results"][0] = dict(bad["results"][1])          # duplicate id
        r = bad["results"][[x["conversation_id"] for x in bad["results"]].index("p1-ru-001")]
        r["criteria"][5]["value"] = 5                         # dialogue has no step 5
        r["criteria"].pop()                                   # critical missing
        bad_path = self.tmp / "bad.json"
        bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        chk = submission.check(str(bad_path), "dev")
        self.assertFalse(chk["ok"])
        text = " ".join(chk["errors"])
        self.assertIn("повторяющиеся", text)
        self.assertIn("dialogue: значение 5", text)
        self.assertIn("ровно по разу", text)
        self.assertFalse(submission.check(str(out), "prod")["ok"])  # prod expects exactly 10 records

    def test_changed_checklist_is_a_clear_error(self):
        doc = ck.apply_ops(ck.load("default@1"), [{"op": "add_criterion", "value": {
            "id": "c09", "title": "Новый", "type": "binary", "weight": 5, "description": "x"}}])
        with self.assertRaises(common.CQAError) as e:
            submission._mapping(doc, submission.profile())
        self.assertIn("c09", str(e.exception))


class SimulationTest(unittest.TestCase):
    """sim.appeals on a copy of the real gold sets; the 'model' answers are fixtures."""

    @classmethod
    def setUpClass(cls):
        from cqa import groundtruth, people

        cls.tmp = Path(tempfile.mkdtemp(prefix="cqa-sim-"))
        shutil.copytree(common.ROOT / "data" / "checklists", cls.tmp / "data" / "checklists")
        shutil.copytree(common.ROOT / "data" / "ground_truth", cls.tmp / "data" / "ground_truth")
        common.use_workspace(cls.tmp)
        state._CACHE.clear()
        cls.gold = groundtruth.gold("dev-gold@v1", "tune")
        cls.ids = sorted(cls.gold["items"])[:4]
        cfg = common.load_config()
        cfg["agents"] = ["m"]
        run.prepare("sim", "dev", "default", "v1", cfg, ids=cls.ids, author="a-01")
        for n, cid in enumerate(cls.ids):  # gold answers with two deliberate errors per conversation
            ans = {k: lab["answer"] for k, lab in cls.gold["items"][cid].items()}
            ans["c05"] = "no" if ans["c05"] == "yes" else "yes"
            ans["c07"] = "yes" if ans["c07"] != "yes" else "no"
            run.submit_response("sim", "m", cid, response(ans), actor="a-01")
        run.ingest("sim", cfg)
        people.seed_demo("dev", "a-01")
        people.confirm_suggestions("a-01")

    @classmethod
    def tearDownClass(cls):
        common.use_workspace(None)
        state._CACHE.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_appeals_from_discrepancies(self):
        from cqa import simulation

        with self.assertRaises(common.CQAError):  # a reviewer may not press the simulation button
            simulation.run("dev-gold@v1", "sim:m", "r-01")
        res = simulation.run("dev-gold@v1", "sim:m", "a-01", reviewers=["r-01", "r-02", "r-03"])
        s = res["stats"]
        self.assertEqual(s["conversations"], 4)
        self.assertEqual(s["discrepancies"], 8)
        self.assertEqual(s["appeals"], 8)
        self.assertEqual(s["by_criterion"], {"c05": 4, "c07": 4})
        st = state.load()
        d = st.decisions[res["items"][0]["decision_id"]]
        self.assertEqual(d["simulated"]["batch"], res["batch"])
        self.assertIn(d["type"], ("A", "B"))
        self.assertEqual(d["reason"], self.gold["items"][d["conversation_id"]][d["criterion_id"]]["comment"])
        ap = st.appeals[res["items"][0]["appeal_id"]]
        self.assertEqual(ap["filed_by"], st.operator_of[ap["conversation_id"]])  # filed by the dialog's operator
        self.assertTrue(ap["simulated"])
        self.assertEqual({i["reviewer"] for i in res["items"]}, {"r-01", "r-02", "r-03"})
        # final answers now equal the gold labels → a second run finds nothing to appeal
        again = simulation.run("dev-gold@v1", "sim:m", "a-01", reviewers=["r-01"])
        self.assertEqual(again["stats"]["discrepancies"], 0)

    def test_noise(self):
        from cqa import simulation

        cfg = common.load_config()
        cfg["agents"] = ["m"]
        run.prepare("sim2", "dev", "default", "v1", cfg, ids=self.ids, author="a-01")
        for cid in self.ids:
            ans = {k: lab["answer"] for k, lab in self.gold["items"][cid].items()}
            ans["c05"] = "no" if ans["c05"] == "yes" else "yes"
            run.submit_response("sim2", "m", cid, response(ans), actor="a-01")
        run.ingest("sim2", cfg)
        res = simulation.run("dev-gold@v1", "sim2:m", "a-01", reviewers=["r-01"], noise=0.5, seed=1)
        wrong = [i for i in res["items"] if i["wrong"]]
        self.assertEqual(res["stats"]["noise_rejected"], len(wrong))
        self.assertTrue(0 < len(wrong) < 4)
        d = state.load().decisions[wrong[0]["decision_id"]]
        self.assertEqual((d["type"], d["action"], d["simulated"]["wrong"]), ("D", "confirm", True))

    def test_before_after(self):
        from cqa import metrics

        cfg = common.load_config()
        cfg["agents"] = ["m"]
        run.prepare("good", "dev", "default", "v1", cfg, ids=self.ids, author="a-01")
        for n, cid in enumerate(self.ids):  # «after»: c05/c07 fixed; c01 broken in the first conversation
            ans = {k: lab["answer"] for k, lab in self.gold["items"][cid].items()}
            if n == 0:
                ans["c01"] = "no" if ans["c01"] == "yes" else "yes"
            run.submit_response("good", "m", cid, response(ans), actor="a-01")
        run.ingest("good", cfg)
        rep = metrics.before_after("dev-gold@v1", "sim:m", "good:m", split="tune", actor="a-01")
        s = rep["summary"]
        self.assertEqual((s["fixed"], s["broken"], s["still_wrong"]), (8, 1, 0))
        self.assertEqual(rep["broken"][0]["criterion_id"], "c01")
        self.assertGreater(s["macro"]["delta_pp"], 0)
        self.assertEqual(rep["criteria"]["c05"]["fixed"], 4)
        md = (self.tmp / rep["paths"]["md"]).read_text(encoding="utf-8") if not Path(rep["paths"]["md"]).is_absolute() \
            else Path(rep["paths"]["md"]).read_text(encoding="utf-8")
        self.assertIn("Исправлено **8**, сломано **1**", md)

    def test_prepare_by_split(self):
        r = api.call("run.prepare", "a-01", {"run_id": "sp", "split": "dev-gold@v1:test", "agents": ["m"],
                                             "template": "v2"})["data"]
        self.assertEqual(r["conversations"], 26)

    def test_type_rule(self):
        from cqa import simulation

        self.assertEqual(simulation._type_for("[буквально по чек-листу] нет прощания"), "B")
        self.assertEqual(simulation._type_for("по правилу R4 обрыв — N/A"), "B")
        self.assertEqual(simulation._type_for("Оператор не поздоровался."), "A")


class PatternsTest(Workspace):
    @staticmethod
    def dec(n, conv, was, became, reviewer, crit="c07", typ="B"):
        return {"decision_id": f"d-{n:04d}", "evaluation_id": f"e{n}", "conversation_id": conv, "criterion_id": crit,
                "model_answer": was, "new_answer": became, "reviewer_id": reviewer, "type": typ,
                "action": "change" if was != became else "confirm", "reason": f"r{n}"}

    def test_confidence_and_thresholds(self):
        from cqa import loop

        convs = ["p1-ru-001", "p1-ru-002", "p1-ru-003", "p1-ru-004", "p1-ru-005", "p1-ru-006"]
        ds = [self.dec(i + 1, c, "no", "na", ["r-01", "r-02", "r-03"][i % 3]) for i, c in enumerate(convs[:5])]
        ds.append(self.dec(6, convs[5], "no", "no", "r-01", typ="D"))          # contradiction: kept «no»
        ds += [self.dec(7 + i, c, 15, 20, "r-01", crit="c04", typ="A") for i, c in enumerate(convs[:3])]
        pats = loop.patterns(ds, state.load())
        base = next(g for g in pats["ready"] if g["label"] == "c07: no → na")
        self.assertEqual((base["cases"], base["contradictions"], len(base["reviewers"])), (5, 1, 3))
        self.assertAlmostEqual(base["agree_share"], 5 / 6, places=3)
        self.assertGreaterEqual(base["wilson_lower"], 0.4)
        self.assertEqual(base["contradicting_decision_ids"], ["d-0006"])
        c04 = next(g for g in pats["accumulating"] if g["label"] == "c04: 15 → 20")
        self.assertIn("проверяющих 1 < 2", c04["not_ready_because"])
        # situation-specific groups only when they split the base group
        self.assertTrue(all(g["signature"]["situation"] == {} or set(g["decision_ids"]) != set(base["decision_ids"])
                            or g["contradictions"] != base["contradictions"]
                            for g in pats["ready"] + pats["accumulating"] if g["signature"]["criterion_id"] == "c07"))

    def test_gold_filter(self):
        from cqa import loop

        real = self.dec(1, "p1-ru-001", "no", "na", "r-01")
        a = {**self.dec(2, "p1-ru-002", "no", "na", "r-01"), "simulated": {"gold": "dev-gold@v1"}}
        b = {**self.dec(3, "p1-ru-003", "no", "na", "r-01"), "simulated": {"gold": "dev-gold-strict@v1"}}
        self.assertEqual([d["decision_id"] for d in (real, a, b) if loop.gold_matches(d, "dev-gold-strict")],
                         ["d-0001", "d-0003"])
        self.assertTrue(loop.gold_matches(a, "dev-gold@v1"))

    def test_analyze_waits_for_patterns(self):
        from cqa import loop

        res = loop.analyze("a-01")
        self.assertIsNone(res["task"])
        self.assertEqual(res["status"], "accumulating")


class JuryScenariosTest(unittest.TestCase):
    """Four jury scenarios + trace, only through api.call (what a frontend would do)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cqa-jury-"))
        shutil.copytree(common.ROOT / "data" / "checklists", cls.tmp / "data" / "checklists")
        common.use_workspace(cls.tmp)
        state._CACHE.clear()

    @classmethod
    def tearDownClass(cls):
        common.use_workspace(None)
        state._CACHE.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_end_to_end(self):
        call = lambda cmd, actor="a-01", **p: api.call(cmd, actor, p)["data"]  # noqa: E731
        call("people.seed_demo", dataset="dev")
        call("people.confirm_suggestions")
        call("run.prepare", run_id="j", ids=IDS, agents=["m"])
        call("run.submit", run_id="j", agent="m", name="p1-ru-001", text=response(BASE))
        gross = {**BASE, "c08": "yes", "c07": "no"}
        call("run.submit", run_id="j", agent="m", name="p1-ru-012", text=response(gross))
        call("run.ingest", run_id="j")
        imp = call("eval.import", source="j:m")
        call("eval.publish", evaluation_ids=imp["evaluation_ids"])

        # 1. gross errors with the flag's explanation
        g = call("jury.gross_errors")
        self.assertEqual([i["conversation_id"] for i in g["items"]], ["p1-ru-012"])
        self.assertEqual(g["items"][0]["explanation"], "t")
        self.assertEqual(g["items"][0]["evidence"][0]["idx"], 1)

        # 2. the operator appeals, a reviewer decides, the final changes, the original stays
        st = state.load()
        op = st.operator_of["p1-ru-012"]
        eid = st.published["p1-ru-012"]
        ap = call("appeal.file", op, evaluation_id=eid, criterion_id="c07", comment="клиент ушёл")["appeal_id"]
        call("appeal.assign", appeal_id=ap, reviewer_id="r-01")
        call("appeal.decide", "r-01", appeal_id=ap, action="change", new_answer="na", reason="обрыв", type="B")
        card = call("conversation.card", conversation_id="p1-ru-012")
        c07 = next(c for c in card["evaluation"]["criteria"] if c["criterion_id"] == "c07")
        self.assertEqual((c07["original"]["answer"], c07["final"]["answer"]), ("no", "na"))

        # 3. add a criterion and change a weight → new version → recompute (LLM only for the new criterion)
        new_crit = {"id": "c09", "title": "Проверка понимания", "type": "binary", "weight": 5, "section": None,
                    "description": "Да — оператор убедился, что клиент понял ответ."}
        res = call("checklist.change", ops=[{"op": "add_criterion", "value": {k: v for k, v in new_crit.items()
                                                                               if v is not None}},
                                            {"op": "set_weight", "criterion_id": "c06", "value": 10}],
                   reason="сценарий жюри", save=True)
        ref = res["saved"]["ref"]
        self.assertEqual(res["preview"]["prompt_affected_criteria"], ["c09"])
        call("version.activate", ref=ref, reason="демо", force=True)
        r1 = call("eval.rescore", ref=ref)
        self.assertEqual(r1["needs_llm"]["criteria"], ["c09"])
        call("run.prepare", run_id="j9", checklist=ref, ids=IDS, agents=["m"], criteria=["c09"])
        text = (common.RUNS / "j9" / "prompts" / "p1-ru-001.md").read_text(encoding="utf-8")
        self.assertIn("c09", text)
        self.assertNotIn("### c01", text)
        for cid in IDS:
            call("run.submit", run_id="j9", agent="m", name=cid, text=response({"c09": "yes"}))
        call("run.ingest", run_id="j9")
        r2 = call("eval.rescore", ref=ref, fill_from="j9:m", publish=True)
        self.assertEqual(r2["needs_llm"]["criteria"], [])
        card = call("conversation.card", conversation_id="p1-ru-012")
        self.assertEqual(card["evaluation"]["checklist"], ref)
        c07 = next(c for c in card["evaluation"]["criteria"] if c["criterion_id"] == "c07")
        self.assertEqual(c07["final"]["answer"], "na")  # the reviewer's decision survives the new version
        self.assertIn("c09", [c["criterion_id"] for c in card["evaluation"]["criteria"]])
        with self.assertRaises(common.CQAError):  # organizers' format has exactly their 8 criteria
            call("results.export", set="dev", path=str(self.tmp / "r.json"))

        # 4 + 7. card with rationale and evidence; trace down to the prompt, raw response and human actions
        c01 = next(c for c in card["evaluation"]["criteria"] if c["criterion_id"] == "c01")
        self.assertEqual(c01["original"]["explanation"], "t")
        tr = call("trace", conversation_id="p1-ru-012", include_text=True)
        self.assertEqual(len(tr["lineage"]), 2)  # rescored evaluation → the LLM original
        self.assertTrue(any(p["prompt_file"] and "### c01" in p["prompt_text"] for p in tr["prompts"]))
        self.assertTrue(any(p.get("response_text") for p in tr["prompts"]))
        self.assertEqual(tr["answers"]["c07"]["decision"]["reason"], "обрыв")
        self.assertEqual(tr["answers"]["c09"]["origin"], "llm:j9:m")
        self.assertEqual(tr["answers"]["c01"]["origin"], "carried")
        self.assertIn("decision_made", [h["type"] for h in tr["human_actions"]])
        self.assertTrue(tr["versions"]["template"])


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
