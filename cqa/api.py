"""Command registry — the single contract for the CLI, the HTTP API and the future frontend (item 7).

Every button of the MC table (and every read a screen needs) is one command: a name, HTTP method, the MC id
it implements, parameters and a handler `fn(actor, **params) -> JSON`. The CLI (`python3 -m cqa <name>
key=value …`) and the HTTP server (`POST /api/v1/<name>`, reads also `GET`) are generated from this table,
so a frontend — including a Dynamics 365 HTML web resource — can be attached without touching the core.
`GET /api/v1/commands` returns this table as JSON.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from . import analytics, appeals, checklist as ck
from . import common, db, evaluations, events, groundtruth, loop, metrics, people, providers
from . import report as base_report
from . import run, sources, state, tasks, versions
from .common import CQAError

COMMANDS: dict = {}


def command(name: str, method: str = "POST", mc: Optional[str] = None, summary: str = "", params: Optional[dict] = None):
    def deco(fn: Callable):
        COMMANDS[name] = {"name": name, "method": method, "mc": mc, "summary": summary, "params": params or {},
                          "fn": fn}
        return fn
    return deco


def P(type_: str = "str", required: bool = False, help: str = "", default=None) -> dict:  # noqa: A002
    return {"type": type_, "required": required, "help": help, "default": default}


UNTIL = P("str", help="ISO time — show the state as of that moment (time machine)")


def describe() -> list:
    return [{k: v for k, v in c.items() if k != "fn"} for c in COMMANDS.values()]


def _coerce(spec: dict, value):
    t = spec["type"]
    if value is None:
        return None
    if t in ("json", "list", "dict") and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            if t == "list":
                return [v for v in value.replace(",", " ").split() if v]
            raise CQAError(f"expected JSON, got {value!r}")
    if t == "int":
        return int(value)
    if t == "float":
        return float(value)
    if t == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if t == "answer":
        if isinstance(value, str) and value.lstrip("-").replace(".", "", 1).isdigit():
            return float(value) if "." in value else int(value)
        return value
    return value


def call(name: str, actor: Optional[str] = None, params: Optional[dict] = None) -> dict:
    """Run one command. Returns {'ok', 'data', 'events'} or raises CQAError."""
    cmd = COMMANDS.get(name)
    if cmd is None:
        raise CQAError(f"unknown command {name!r}", "not_found")
    params = dict(params or {})
    actor = params.pop("actor", None) or actor or common.setting("actors.cli_default", "a-01")
    unknown = set(params) - set(cmd["params"])
    if unknown:
        raise CQAError(f"{name}: unknown parameters {sorted(unknown)}; expected {sorted(cmd['params'])}")
    kwargs = {}
    for pname, spec in cmd["params"].items():
        if pname in params:
            kwargs[pname] = _coerce(spec, params[pname])
        elif spec["required"]:
            raise CQAError(f"{name}: parameter {pname!r} is required")
        elif spec["default"] is not None:
            kwargs[pname] = spec["default"]
    before = events.fingerprint()
    seq_before = len(events.read()) if cmd["method"] == "POST" else None
    data = cmd["fn"](actor, **kwargs)
    new_events = []
    if cmd["method"] == "POST" and events.fingerprint() != before:
        new_events = [e["id"] for e in events.read()[seq_before:]]
    return {"ok": True, "data": data, "events": new_events}


# =============================== system ===============================

@command("commands", "GET", summary="This table: every command with method, MC id and parameters")
def _commands(actor):
    return describe()


@command("whoami", "GET", summary="Role of the actor (roles come from the people directory or the id prefix)")
def _whoami(actor):
    st = state.load()
    return {"actor": actor, "role": st.role_of(actor), "person": st.people.get(actor)}


@command("history.feed", "GET", summary="History table (H.7) with slices (H.8)",
         params={"conversation_id": P(), "criterion_id": P(), "type": P(), "actor_id": P(), "limit": P("int", default=200),
                 "until": UNTIL})
def _feed(actor, conversation_id=None, criterion_id=None, type=None, actor_id=None, limit=200, until=None):  # noqa: A002
    st = state.load(until)
    rows = [state.feed_row(e) for e in st.events]
    rows = [r for r in rows if (not conversation_id or r["conversation_id"] == conversation_id)
            and (not criterion_id or r["criterion_id"] == criterion_id) and (not type or r["type"] == type)
            and (not actor_id or r["actor"] == actor_id)]
    return rows[-limit:]


@command("history.verify", "GET", summary="Check the hash chain of the event log (VC.12)")
def _verify(actor):
    return events.verify()


@command("history.at", "GET", summary="Time machine (VC.9): active versions and counts at a moment",
         params={"until": P(required=True)})
def _at(actor, until):
    st = state.load(until)
    return {"at": until, "active_checklists": {cid: ck.active_version(cid, until=until) for cid in ck.ids()},
            "evaluations": len(st.evaluations), "appeals": len(st.appeals), "decisions": len(st.decisions),
            "precedents": len(st.precedent), "proposals": len(st.proposals), "events": len(st.events)}


@command("db.query", "GET", summary="Read-only SQL over the star-schema projection",
         params={"sql": P(required=True), "until": UNTIL})
def _query(actor, sql, until=None):
    return db.query(sql, until=until)


@command("export", "GET", mc="MC.13", summary="Export history / evaluations / appeals / decisions as rows",
         params={"what": P(required=True, help="feed | evaluations | criteria | appeals | decisions | proposals"),
                 "until": UNTIL})
def _export(actor, what, until=None):
    tables = {"feed": "feed", "evaluations": "fact_evaluation", "criteria": "fact_criterion",
              "appeals": "fact_appeal", "decisions": "fact_decision", "proposals": "fact_proposal"}
    if what not in tables:
        raise CQAError(f"what must be one of {sorted(tables)}")
    return db.query(f"SELECT * FROM {tables[what]}", until=until)


# =============================== data and people ===============================

@command("dataset.list", "GET", summary="Conversations of a dataset with operator and latest evaluation",
         params={"dataset": P(default="dev"), "until": UNTIL})
def _dataset_list(actor, dataset="dev", until=None):
    from . import conversation as cv

    st = state.load(until)
    out = []
    for cid in cv.list_ids(dataset):
        conv = cv.load(dataset, cid)
        eid = st.published.get(cid)
        ev = st.evaluations.get(eid) if eid else None
        out.append({"conversation_id": cid, "channel": conv["channel"], "language": conv["language"],
                    "turns": len(conv["transcript"]), "operator_id": st.operator_of.get(cid),
                    "published_evaluation": eid, "final_total": ev and st.final(eid)["final"]["total"],
                    "open_appeals": sum(1 for a in st.appeals.values()
                                        if a["conversation_id"] == cid and a["status"] in ("open", "assigned"))})
    return out


@command("dataset.import", mc="MC.1", summary="Upload transcripts in the organizers' API format",
         params={"dataset": P(required=True), "records": P("json", required=True)})
def _dataset_import(actor, dataset, records):
    from . import access

    access.require(actor, "dataset.manage")
    return run.import_dataset(dataset, records if isinstance(records, list) else [records], actor)


@command("conversation.card", "GET", summary="Card: transcript, criteria with evidence, decisions, final, history",
         params={"conversation_id": P(required=True), "evaluation_id": P(), "viewer": P(), "until": UNTIL})
def _card(actor, conversation_id, evaluation_id=None, viewer=None, until=None):
    return evaluations.card(conversation_id, evaluation_id, viewer or actor, until)


@command("people.list", "GET", params={"role": P(), "until": UNTIL})
def _people(actor, role=None, until=None):
    return people.directory(role, until)


@command("people.register", params={"person_id": P(required=True), "name": P(required=True),
                                    "role": P(required=True), "team": P(), "languages": P("list")})
def _people_reg(actor, person_id, name, role, team=None, languages=None):
    return people.register(person_id, name, role, actor, team, languages)


@command("people.update", params={"person_id": P(required=True), "fields": P("json", required=True)})
def _people_upd(actor, person_id, fields):
    return people.update(person_id, fields, actor)


@command("people.seed_demo", summary="Demo directory (marked demo) + operator suggestions by name (PE.5)",
         params={"dataset": P(default="dev")})
def _seed(actor, dataset="dev"):
    return people.seed_demo(dataset, actor)


@command("people.assign", mc="MC.2", summary="«Подтвердить оператора» for one conversation",
         params={"conversation_id": P(required=True), "operator_id": P(required=True)})
def _assign_op(actor, conversation_id, operator_id):
    return people.assign(conversation_id, operator_id, actor)


@command("people.confirm_suggestions", mc="MC.2", summary="Confirm suggested operators (bulk button)",
         params={"conversation_ids": P("list")})
def _confirm(actor, conversation_ids=None):
    return people.confirm_suggestions(actor, conversation_ids)


# =============================== checklist and versions ===============================

@command("checklist.list", "GET", params={"until": UNTIL})
def _ck_list(actor, until=None):
    out = []
    for cid in ck.ids():
        for v in ck.versions(cid):
            d = ck.load(f"{cid}@{v}")
            m = d["meta"]
            out.append({"ref": ck.make_ref(cid, v), "status": ck.status_of(cid, v, until), "created_at": m.get("created_at"),
                        "author": m.get("author"), "reason": m.get("reason"), "parent": m.get("parent_version"),
                        "content_hash": m["content_hash"], "criteria": len(ck.active_criteria(d))})
    return out


@command("checklist.get", "GET", params={"ref": P(default="default")})
def _ck_get(actor, ref="default"):
    d = ck.load(ref)
    return {**d, "criteria_revs": ck.criterion_revs(d)}


@command("checklist.diff", "GET", mc="VC.10", params={"a": P(required=True), "b": P(required=True)})
def _ck_diff(actor, a, b):
    da, db_ = ck.load(a), ck.load(b)
    return {"lines": ck.diff(da, db_), "struct": ck.diff_struct(da, db_)}


@command("checklist.revisions", "GET", params={"checklist_id": P(default="default")})
def _ck_revs(actor, checklist_id="default"):
    return ck.revisions(checklist_id)


@command("checklist.preview", "GET", mc="MC.4", summary="Consequences of edit ops before saving (CH.5)",
         params={"ops": P("json", required=True), "base": P()})
def _ck_preview(actor, ops, base=None):
    doc = ck.load(base or "default")
    return loop.preview(doc, ck.apply_ops(doc, ops))


@command("checklist.change", summary="Edit through the interface: ops → preview, or a new draft version (save=true)",
         params={"ops": P("json", required=True), "reason": P(), "base": P(), "save": P("bool", default=False)})
def _ck_change(actor, ops, reason="", base=None, save=False):
    return loop.change_checklist(actor, ops, reason, base, save)


@command("checklist.save", summary="Save a whole edited checklist document as the next version",
         params={"document": P("json", required=True), "checklist_id": P(default="default"),
                 "reason": P(required=True)})
def _ck_save(actor, document, reason, checklist_id="default"):
    from . import access

    access.require(actor, "checklist.change")
    return ck.save_doc(document, checklist_id, actor, reason)


@command("version.activate", mc="MC.11", summary="«Активировать» / «Откатить» (activate an older version)",
         params={"ref": P(required=True), "reason": P(default=""), "profile": P(default="default"),
                 "force": P("bool", default=False)})
def _activate(actor, ref, reason="", profile="default", force=False):
    return loop.activate(ref, actor, reason, profile, force)


@command("version.archive", summary="Archive a version (nothing is deleted, VC.1) or restore it",
         params={"ref": P(required=True), "reason": P(required=True), "restore": P("bool", default=False)})
def _archive(actor, ref, reason, restore=False):
    return loop.archive(ref, actor, reason, restore)


@command("versions.list", "GET", summary="Registered versions of templates, params, features, models",
         params={"kind": P(), "name": P(), "until": UNTIL})
def _versions(actor, kind=None, name=None, until=None):
    return versions.history(kind, name, until)


@command("versions.diff", "GET", params={"kind": P(required=True), "name": P(required=True),
                                         "a": P("int", required=True), "b": P("int", required=True)})
def _versions_diff(actor, kind, name, a, b):
    return versions.diff(kind, name, a, b)


@command("params.get", "GET")
def _params_get(actor):
    return {"pipeline": common.load_config(), "platform": common.platform_config(), "providers": providers.config()}


@command("params.set", summary="Change one pipeline parameter (new params version, logged)",
         params={"key": P(required=True), "value": P("json", required=True), "reason": P(required=True)})
def _params_set(actor, key, value, reason):
    from . import access

    access.require(actor, "params.change")
    return versions.set_param(key, value, actor, reason)


# =============================== evaluation ===============================

@command("run.prepare", mc="MC.3", summary="«Оценить»: build prompts (S1–S3) for a set of conversations",
         params={"run_id": P(required=True), "dataset": P(default="dev"), "checklist": P(default="default"),
                 "variant": P(default="v1"), "ids": P("list"), "sample": P("int", default=0),
                 "agents": P("list"), "template": P(),
                 "precedents": P("bool", default=True, help="insert selected precedents into {{PRECEDENTS}}"),
                 "criteria": P("list", help="only these criteria (partial re-evaluation after a checklist change)"),
                 "split": P(help="conversations of a gold split instead of ids, e.g. dev-gold@v1:test")})
def _prepare(actor, run_id, dataset="dev", checklist="default", variant="v1", ids=None, sample=0, agents=None,
             template=None, precedents=True, criteria=None, split=None):
    from . import access

    access.require(actor, "evaluation.run")
    if split:
        set_spec, _, part = split.rpartition(":")
        if not set_spec:
            raise CQAError("split must look like dev-gold@v1:test")
        ids = sorted(groundtruth.gold(set_spec, part)["items"])
    cfg = common.load_config()
    if agents:
        cfg["agents"] = agents
    if template:
        cfg["template"] = template
    run_dir = run.prepare(run_id, dataset, checklist, variant, cfg, ids=ids, sample=sample, author=actor,
                          precedents=precedents, criteria=criteria)
    m = common.load_json(run_dir / "manifest.json")
    return {"run_id": run_id, "conversations": len(m["conversations"]), "agents": m["agents"],
            "checklist": m["checklist"], "versions": m["versions"], "projection": m["token_projection"]}


@command("run.status", "GET", params={"run_id": P(required=True)})
def _run_status(actor, run_id):
    return run.status(run_id)


@command("run.prompt", "GET", summary="Text of one prompt to paste into a chat",
         params={"run_id": P(required=True), "name": P(required=True)})
def _run_prompt(actor, run_id, name):
    return run.prompt_text(run_id, name)


@command("run.submit", summary="Paste a model answer for one prompt (manual provider)",
         params={"run_id": P(required=True), "agent": P(required=True), "name": P(required=True),
                 "text": P(required=True), "usage": P("json")})
def _run_submit(actor, run_id, agent, name, text, usage=None):
    from . import access

    access.require(actor, "evaluation.run")
    return run.submit_response(run_id, agent, name, text, usage, actor)


@command("sim.appeals", summary="Simulated reviewer: every discrepancy of a source with the gold labels on a split "
         "becomes an operator appeal + reviewer decision = gold label (marked simulated)",
         params={"gold": P(required=True, help="gold set, e.g. dev-gold@v1"), "source": P(required=True, help="run:agent"),
                 "split": P(default="tune"), "reviewers": P("list", help="default: first three reviewers"),
                 "noise": P("float", default=0.0, help="share of deliberately wrong decisions"),
                 "seed": P("int", default=7), "limit": P("int", default=0), "criteria": P("list")})
def _sim_appeals(actor, gold, source, split="tune", reviewers=None, noise=0.0, seed=7, limit=0, criteria=None):
    from . import simulation

    return simulation.run(gold, source, actor, split, reviewers, noise, seed, limit, criteria)


@command("jury.gross_errors", "GET", summary="Dialogs with a gross error: flag explanation, evidence turns, total",
         params={"source": P(help="run:agent; default published"), "until": UNTIL})
def _gross(actor, source=None, until=None):
    from . import jury

    return jury.gross_errors(source, until)


@command("eval.rescore", summary="New checklist version → recompute published evaluations without LLM; new or "
         "reworded criteria from fill_from (a run prepared with criteria=…)",
         params={"ref": P(required=True, help="checklist version, e.g. default@v3"), "fill_from": P(),
                 "publish": P("bool", default=False), "ids": P("list")})
def _rescore(actor, ref, fill_from=None, publish=False, ids=None):
    from . import jury

    return jury.rescore(ref, actor, fill_from, publish, ids)


@command("trace", "GET", summary="Where an answer came from: exact prompt and raw response files, all versions "
         "(checklist, template, params, rule clarifications, precedents) and every human action",
         params={"conversation_id": P(required=True), "evaluation_id": P(), "criterion_id": P(),
                 "include_text": P("bool", default=False), "until": UNTIL})
def _trace(actor, conversation_id, evaluation_id=None, criterion_id=None, include_text=False, until=None):
    from . import jury

    return jury.trace(conversation_id, evaluation_id, criterion_id, include_text, until)


@command("results.export", mc="S8.4", summary="Write results/<dev|prod>/results.json in the organizers' format "
         "(schema 1.0) and check it", params={"set": P(default="dev", help="dev | prod"), "dataset": P(),
                                              "source": P(help="run:agent; default published + reviewer decisions"),
                                              "profile": P(), "model_name": P(), "model_mode": P(), "path": P()})
def _results_export(actor, set="dev", dataset=None, source=None, profile=None, model_name=None,  # noqa: A002
                    model_mode=None, path=None):
    from . import access, submission

    access.require(actor, "export")
    return submission.export(set, dataset, source, profile, actor, path, model_name, model_mode)


@command("results.check", "GET", summary="Pre-push check of a results.json: UTF-8, schema, 8 criteria once, value "
         "types, unique ids, expected count", params={"path": P(required=True), "set": P(), "profile": P(),
                                                       "expected": P("int")})
def _results_check(actor, path, set=None, profile=None, expected=None):  # noqa: A002
    from . import submission

    return submission.check(path, set, profile, expected)


@command("metrics.before_after", summary="Slide «до и после»: two sources on the held-out split — accuracy "
         "change, McNemar, labels fixed / broken / still wrong", params={
             "gold": P(required=True), "before": P(required=True, help="run:agent before the loop"),
             "after": P(required=True, help="run:agent after the loop"), "split": P(default="test"),
             "save": P("bool", default=True)})
def _before_after(actor, gold, before, after, split="test", save=True):
    return metrics.before_after(gold, before, after, split, actor, save)


@command("review.queue", "GET", summary="Answers to show a reviewer first: insufficient_data, ambiguities, low "
         "confidence, failed evidence check, models disagree", params={"source": P(help="run:agent; default published"),
                                                                      "reasons": P("list"), "until": UNTIL})
def _review_queue(actor, source=None, reasons=None, until=None):
    return evaluations.review_queue(source, reasons, until)


@command("paste.status", "GET", summary="Manual paste: done / waiting prompts of any run folder (also variants/*/runs)",
         params={"run": P(required=True), "agent": P()})
def _paste_status(actor, run, agent=None):
    from . import manual

    return manual.status(run, agent)


@command("paste.next", "GET", summary="Manual paste: the next prompt without a response (text to paste into a fresh chat)",
         params={"run": P(required=True), "agent": P()})
def _paste_next(actor, run, agent=None):
    from . import manual

    return manual.current(run, agent)


@command("paste.save", summary="Manual paste: check the model answer at once and save it to the current prompt",
         params={"run": P(required=True), "text": P(required=True), "agent": P(), "name": P(),
                 "force": P("bool", default=False, help="save even if the parser rejects it")})
def _paste_save(actor, run, text, agent=None, name=None, force=False):
    from . import access, manual

    access.require(actor, "evaluation.run")
    return manual.save(run, text, agent, name, force, actor)


@command("run.dispatch", mc="MC.3", summary="Send waiting prompts through the agent's provider (CLI/API when enabled)",
         params={"run_id": P(required=True), "agent": P(required=True), "provider": P(), "limit": P("int", default=0)})
def _run_dispatch(actor, run_id, agent, provider=None, limit=0):
    from . import access

    access.require(actor, "evaluation.run")
    return run.dispatch(run_id, agent, provider, limit, actor)


@command("run.ingest", summary="S5–S7: validate pasted answers and score", params={"run_id": P(required=True),
                                                                                  "agents": P("list")})
def _run_ingest(actor, run_id, agents=None):
    return run.ingest(run_id, common.load_config(), agents)


@command("run.report", summary="Agents head-to-head report of a run", params={"run_id": P(required=True)})
def _run_report(actor, run_id):
    return base_report.build(run_id, common.load_config())


@command("eval.aggregate", summary="S6: ensemble of the run's agents → validated/majority",
         params={"run_id": P(required=True), "agents": P("list"), "name": P(default="majority")})
def _aggregate(actor, run_id, agents=None, name="majority"):
    return evaluations.aggregate(run_id, agents, name, actor)


@command("eval.import", summary="Import results of a source (run:agent, variant:run:agent, folder) into the history",
         params={"source": P(required=True), "ids": P("list")})
def _import(actor, source, ids=None):
    from . import access

    access.require(actor, "evaluation.run")
    return evaluations.import_source(source, actor, ids)


@command("eval.publish", summary="Make evaluations official (what operators see and analytics count)",
         params={"evaluation_ids": P("list"), "source": P()})
def _publish(actor, evaluation_ids=None, source=None):
    from . import access

    access.require(actor, "evaluation.publish")
    if source:
        return evaluations.publish_source(source, actor)
    return evaluations.publish(evaluation_ids or [], actor)


@command("eval.list", "GET", params={"conversation_id": P(), "run_id": P(), "agent": P(), "until": UNTIL})
def _eval_list(actor, conversation_id=None, run_id=None, agent=None, until=None):
    st = state.load(until)
    return [{k: e.get(k) for k in ("evaluation_id", "run_id", "agent", "conversation_id", "checklist", "total",
                                   "gross_error", "status", "current", "published", "imported_at")}
            for e in st.evaluations.values() if (not conversation_id or e["conversation_id"] == conversation_id)
            and (not run_id or e["run_id"] == run_id) and (not agent or e["agent"] == agent)]


@command("eval.final", "GET", summary="Original, decisions and final of one evaluation",
         params={"evaluation_id": P(required=True), "until": UNTIL})
def _final(actor, evaluation_id, until=None):
    return state.load(until).final(evaluation_id)


# =============================== appeals ===============================

@command("appeal.file", summary="«Оспорить»: criterion + comment", params={
    "evaluation_id": P(required=True), "criterion_id": P(required=True), "comment": P(required=True),
    "operator_id": P(), "turn_idx": P("int"), "attachments": P("json")})
def _appeal_file(actor, evaluation_id, criterion_id, comment, operator_id=None, turn_idx=None, attachments=None):
    return appeals.file(evaluation_id, criterion_id, comment, actor, operator_id, turn_idx, attachments)


@command("appeal.assign", mc="MC.5", params={"appeal_id": P(required=True), "reviewer_id": P(required=True)})
def _appeal_assign(actor, appeal_id, reviewer_id):
    return appeals.assign(appeal_id, reviewer_id, actor)


@command("appeal.view", summary="Reviewer's view: criterion text of the evaluation's version, comment, fragment, "
                                "model answer (hidden in blind mode until reveal)",
         params={"appeal_id": P(required=True), "reveal": P("bool", default=False)})
def _appeal_view(actor, appeal_id, reveal=False):
    return appeals.view(appeal_id, actor, reveal)


@command("appeal.list", "GET", params={"status": P(), "reviewer": P(), "operator": P(), "until": UNTIL})
def _appeal_list(actor, status=None, reviewer=None, operator=None, until=None):
    return appeals.list_appeals(status, reviewer, operator, until)


@command("appeal.decide", summary="«Подтвердить» / «Изменить»: answer, reason, type A–D (ground truth)", params={
    "appeal_id": P(), "evaluation_id": P(help="audit decision without appeal"), "criterion_id": P(),
    "action": P(required=True, help="confirm | change"), "new_answer": P("answer"),
    "reason": P(required=True), "type": P(required=True, help="A | B | C | D"),
    "overall_score": P("float"), "time_spent_s": P("float"), "revise": P("bool", default=False)})
def _decide(actor, action, reason, type, appeal_id=None, evaluation_id=None, criterion_id=None,  # noqa: A002
            new_answer=None, overall_score=None, time_spent_s=None, revise=False):
    return appeals.decide(actor, action, reason, type, appeal_id, evaluation_id, criterion_id, new_answer,
                          overall_score, time_spent_s, revise)


@command("appeal.close", params={"appeal_id": P(required=True), "reason": P(required=True)})
def _appeal_close(actor, appeal_id, reason):
    return appeals.close(appeal_id, reason, actor)


@command("appeal.conflicts", "GET", summary="Different reviewers, different answers on the same case (HU.6)",
         params={"until": UNTIL})
def _conflicts(actor, until=None):
    return appeals.conflicts(until)


@command("reviewer.settings", mc="MC.12", summary="Blind mode switch (HU.5)",
         params={"reviewer_id": P(), "blind": P("bool")})
def _rev_settings(actor, reviewer_id=None, blind=None):
    return appeals.set_reviewer_settings(reviewer_id or actor, actor, blind)


@command("appeal.counterargument", mc="MC.6", summary="«Аргументы модели»: create an LLM task (manual provider)",
         params={"appeal_id": P(required=True), "reviewer_reason": P()})
def _counter(actor, appeal_id, reviewer_reason=None):
    ap = state.load().appeal(appeal_id)
    return tasks.create("counterargument", actor, {"appeal_id": appeal_id, "reviewer_reason": reviewer_reason,
                                                   "conversation_id": ap["conversation_id"],
                                                   "criterion_id": ap["criterion_id"]})


@command("appeal.suggest_type", summary="Hint for type A–D by the orchestrator (AP.5), reviewer confirms",
         params={"appeal_id": P(required=True), "reviewer_reason": P(required=True)})
def _suggest(actor, appeal_id, reviewer_reason):
    ap = state.load().appeal(appeal_id)
    return tasks.create("suggest_appeal_type", actor, {"appeal_id": appeal_id, "reviewer_reason": reviewer_reason,
                                                       "conversation_id": ap["conversation_id"],
                                                       "criterion_id": ap["criterion_id"]})


@command("precedent.mark", mc="MC.7", summary="«Использовать как прецедент» (or on=false to withdraw)",
         params={"decision_id": P(required=True), "note": P(default=""), "on": P("bool", default=True)})
def _precedent(actor, decision_id, note="", on=True):
    return appeals.mark_precedent(decision_id, actor, note, on)


@command("precedent.list", "GET", params={"criterion_id": P(), "until": UNTIL})
def _precedents(actor, criterion_id=None, until=None):
    return appeals.precedents(criterion_id, until)


@command("precedent.render", "GET", summary="Precedents block for a prompt (mechanism A, HX.6)",
         params={"conversation_id": P(required=True), "criteria": P("list"), "dataset": P(default="dev"),
                 "k": P("int")})
def _precedent_render(actor, conversation_id, criteria=None, dataset="dev", k=None):
    from . import conversation as cv

    conv = cv.load(dataset, conversation_id)
    crit = criteria or [c["id"] for c in ck.active_criteria(ck.load("default"))]
    items = appeals.select_precedents(conv, crit, k)
    return {"items": items, "text": appeals.render_precedents(items)}


# =============================== learning loop ===============================

@command("loop.digest", mc="MC.8", summary="Algorithmic extract of the history (S10.2) — what the orchestrator sees",
         params={"gold": P(help="only simulated decisions of this gold set + real ones")})
def _digest(actor, gold=None):
    return loop.build_digest(actor, gold=gold)


@command("loop.analyze", mc="MC.8", summary="«Проанализировать историю»: digest → orchestrator task (proposals)",
         params={"digest_id": P(), "provider": P(),
                 "force": P("bool", default=False, help="create the task even if no pattern is above the thresholds"),
                 "gold": P(help="only simulated decisions of this gold set (e.g. dev-gold-strict) + real ones")})
def _analyze(actor, digest_id=None, provider=None, force=False, gold=None):
    return loop.analyze(actor, digest_id, provider, force, gold)


@command("loop.patterns", "GET", summary="Patterns «criterion × was → became × situation» with confidence: ready "
         "(above thresholds) and accumulating", params={"gold": P(help="only simulated decisions of this gold set + real")})
def _patterns(actor, gold=None):
    return loop.patterns([d for d in state.load().decisions.values() if not gold or loop.gold_matches(d, gold)])


@command("checklist.request_edit", summary="Change in plain words → orchestrator task → proposal (K.7)",
         params={"request": P(required=True), "base": P()})
def _req_edit(actor, request, base=None):
    return tasks.create("checklist_edit", actor, {"request": request, "base": base})


@command("task.list", "GET", params={"kind": P(), "status": P(), "until": UNTIL})
def _task_list(actor, kind=None, status=None, until=None):
    return tasks.listing(kind, status, until)


@command("task.prompt", "GET", params={"task_id": P(required=True)})
def _task_prompt(actor, task_id):
    return tasks.prompt(task_id)


@command("task.submit", summary="Paste the orchestrator's answer and parse it",
         params={"task_id": P(required=True), "text": P(required=True)})
def _task_submit(actor, task_id, text):
    return tasks.submit(task_id, text, actor)


@command("task.ingest", summary="Parse a response file already saved next to the prompt",
         params={"task_id": P(required=True)})
def _task_ingest(actor, task_id):
    return tasks.ingest(task_id, actor)


@command("task.dispatch", summary="Send a task through its provider (when not manual)",
         params={"task_id": P(required=True)})
def _task_dispatch(actor, task_id):
    return tasks.dispatch(task_id, actor)


@command("task.view", summary="Open a task result (viewing a counter-argument is recorded, HU.4)",
         params={"task_id": P(required=True)})
def _task_view(actor, task_id):
    return tasks.view(task_id, actor)


@command("proposal.list", "GET", params={"status": P(), "until": UNTIL})
def _proposals(actor, status=None, until=None):
    return [p for p in state.load(until).proposals.values() if not status or p["status"] == status]


@command("proposal.create", summary="A human proposal (same format as the orchestrator's)",
         params={"kind": P(required=True), "title": P(required=True), "ops": P("json", required=True),
                 "rationale": P(default=""), "base": P(), "criterion_id": P(), "evidence_decisions": P("list")})
def _proposal_create(actor, kind, title, ops, rationale="", base=None, criterion_id=None, evidence_decisions=None):
    return loop.create_proposal(actor, kind, title, ops, rationale, base, criterion_id,
                                evidence_decisions=evidence_decisions)


@command("proposal.preview", "GET", summary="Consequences of a proposal if accepted",
         params={"proposal_id": P(required=True)})
def _proposal_preview(actor, proposal_id):
    p = loop.proposal(proposal_id)
    base = ck.load(p["base"])
    return loop.preview(base, ck.apply_ops(base, p["ops"]))


@command("proposal.accept", mc="MC.9", summary="«Принять» (optionally with edited ops) → new draft version",
         params={"proposal_id": P(required=True), "reason": P(default=""), "ops": P("json")})
def _accept(actor, proposal_id, reason="", ops=None):
    return loop.accept(proposal_id, actor, reason, ops)


@command("proposal.reject", mc="MC.9", params={"proposal_id": P(required=True), "reason": P(required=True)})
def _reject(actor, proposal_id, reason):
    return loop.reject(proposal_id, actor, reason)


@command("regression.run", mc="MC.10", summary="«Проверить на эталоне»: candidate vs baseline + decided appeals",
         params={"candidate": P(required=True), "baseline": P(), "gold": P(), "split": P(default="test"),
                 "include_decisions": P("bool")})
def _regression(actor, candidate, baseline=None, gold=None, split="test", include_decisions=None):
    return loop.regression(candidate, actor, baseline, gold, split, include_decisions)


@command("simulate.weights", "GET", summary="What-if: totals with other weights / version, 0 LLM calls",
         params={"weights": P("json"), "checklist": P(), "source": P(), "use_final": P("bool", default=True)})
def _simulate(actor, weights=None, checklist=None, source=None, use_final=True):
    return loop.simulate(weights, checklist, source, use_final)


# =============================== ground truth and metrics ===============================

@command("gt.list", "GET")
def _gt_list(actor):
    return groundtruth.sets()


@command("gt.get", "GET", params={"set": P(required=True)})
def _gt_get(actor, set):  # noqa: A002
    return groundtruth.load(set)


@command("gt.draft", summary="Draft labels from several agents: consensus pre-filled, disagreements for people",
         params={"sources": P("list", required=True), "set": P(required=True), "ids": P("list"), "checklist": P()})
def _gt_draft(actor, sources, set, ids=None, checklist=None):  # noqa: A002
    from . import access

    access.require(actor, "gt.manage")
    return groundtruth.draft(sources, set, ids, checklist, actor)


@command("gt.import", summary="Import a reviewed draft / JSON / CSV as a new immutable version",
         params={"path": P(required=True), "set": P(required=True), "reason": P(required=True),
                 "allow_partial": P("bool", default=False), "checklist": P()})
def _gt_import(actor, path, set, reason, allow_partial=False, checklist=None):  # noqa: A002
    from . import access

    access.require(actor, "gt.manage")
    return groundtruth.import_file(path, set, actor, reason, allow_partial, checklist)


@command("gt.update", summary="Change labels → new version", params={
    "set": P(required=True), "labels": P("json", required=True), "reason": P(required=True)})
def _gt_update(actor, set, labels, reason):  # noqa: A002
    from . import access

    access.require(actor, "gt.manage")
    return groundtruth.update_labels(set, labels, actor, reason)


@command("metrics.compare", summary="Accuracy of sources vs ground truth, head to head (+ report file)",
         params={"gold": P(required=True, help="set@vN or 'decisions'"), "sources": P("list", required=True),
                 "split": P(default="all"), "save": P("bool", default=True)})
def _metrics(actor, gold, sources, split="all", save=True):
    return metrics.compare(gold, sources, split, actor, save)


@command("metrics.agreement", "GET", summary="Agreement between sources: stability, annotators, interference",
         params={"sources": P("list", required=True), "criteria": P("list")})
def _agreement(actor, sources, criteria=None):
    return metrics.agreement(sources, criteria)


@command("sources.resolve", "GET", params={"source": P(required=True)})
def _src(actor, source):
    s = sources.resolve(source)
    return {"label": s["label"], "dir": common.rel(s["dir"]), "run_id": s["run_id"], "agent": s["agent"],
            "checklist": (s["manifest"] or {}).get("checklist")}


# =============================== analytics ===============================

@command("analytics.overview", "GET", params={"until": UNTIL})
def _ov(actor, until=None):
    return analytics.overview(until)


@command("analytics.operators", "GET", params={"until": UNTIL})
def _ops(actor, until=None):
    return analytics.operators(until)


@command("analytics.reviewers", "GET", params={"until": UNTIL})
def _revs(actor, until=None):
    return analytics.reviewers(until)


@command("analytics.pairs", "GET", params={"until": UNTIL})
def _pairs(actor, until=None):
    return analytics.pairs(until)


@command("analytics.criteria", "GET", params={"until": UNTIL})
def _crits(actor, until=None):
    return analytics.criteria(until)


@command("analytics.model_errors", "GET", params={"by": P(default="language"), "until": UNTIL})
def _model_err(actor, by="language", until=None):
    return analytics.model_errors(by, until)


@command("analytics.relate", "GET", params={"x": P(required=True), "y": P(required=True), "until": UNTIL})
def _relate(actor, x, y, until=None):
    return analytics.relate(x, y, until)


@command("providers.list", "GET")
def _providers(actor):
    cfg = providers.config()
    return {"default": cfg.get("default"), "agents": cfg.get("agents"),
            "providers": [providers.get(n).describe() for n in cfg.get("providers", {})]}
