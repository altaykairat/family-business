"""Small append-only audit trail around current evaluation snapshots."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from engine import blank_answers, normalize_conversation, validate_checklist, score, Invalid


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path, default_checklist: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS checklists (
                  id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                  created_at TEXT NOT NULL, PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, group_id TEXT, body TEXT NOT NULL, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations (
                  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, checklist_id TEXT NOT NULL,
                  checklist_version INTEGER NOT NULL, checklist_snapshot TEXT NOT NULL,
                  answers TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
                  FOREIGN KEY(conversation_id) REFERENCES conversations(id));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL,
                  kind TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS appeals (
                  id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL, criterion_id TEXT NOT NULL,
                  comment TEXT NOT NULL, status TEXT NOT NULL, decision TEXT,
                  created_at TEXT NOT NULL, decided_at TEXT);
            """)
            count = db.execute("SELECT count(*) FROM checklists").fetchone()[0]
            if not count:
                checklist = json.loads(default_checklist.read_text(encoding="utf-8"))
                validate_checklist(checklist)
                db.execute("INSERT INTO checklists VALUES (?,?,?,?)",
                           (checklist["id"], checklist["version"], encode(checklist), stamp()))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def latest_checklist(self, checklist_id="default"):
        with self.connect() as db:
            row = db.execute("SELECT body FROM checklists WHERE id=? ORDER BY version DESC LIMIT 1",
                             (checklist_id,)).fetchone()
        if row is None:
            raise Invalid("Checklist not found")
        return json.loads(row[0])

    def checklist(self, checklist_id, version):
        with self.connect() as db:
            row = db.execute("SELECT body FROM checklists WHERE id=? AND version=?",
                             (checklist_id, version)).fetchone()
        if row is None:
            raise Invalid("Checklist version not found")
        return json.loads(row[0])

    def checklist_versions(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, version, created_at FROM checklists ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def save_checklist(self, body):
        validate_checklist(body)
        if not isinstance(body.get("id"), str) or not body["id"]:
            raise Invalid("Checklist ID is required")
        with self.connect() as db:
            last = db.execute("SELECT max(version) FROM checklists WHERE id=?", (body["id"],)).fetchone()[0]
            body = {**body, "version": (last or 0) + 1}
            db.execute("INSERT INTO checklists VALUES (?,?,?,?)",
                       (body["id"], body["version"], encode(body), stamp()))
        return body

    def import_conversations(self, records, group_id="UPLOAD"):
        if not isinstance(records, list) or not records:
            raise Invalid("Expected a nonempty list of conversations")
        normalized = [normalize_conversation(record) for record in records]
        with self.connect() as db:
            for record in normalized:
                existing = db.execute("SELECT body FROM conversations WHERE id=?",
                                      (record["conversation_id"],)).fetchone()
                if existing and json.loads(existing[0]) != record:
                    raise Invalid(f"{record['conversation_id']}: already imported with different transcript")
                if not existing:
                    db.execute("INSERT INTO conversations VALUES (?,?,?,?)",
                               (record["conversation_id"], group_id, encode(record), stamp()))
        return {"imported": len(normalized)}

    def conversations(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, group_id, body FROM conversations ORDER BY id").fetchall()
        return [{"id": row["id"], "group_id": row["group_id"],
                 "language": json.loads(row["body"]).get("language"),
                 "channel": json.loads(row["body"]).get("channel")} for row in rows]

    def conversation(self, cid):
        with self.connect() as db:
            row = db.execute("SELECT body FROM conversations WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise Invalid("Conversation not found")
        return json.loads(row[0])

    def create_evaluation(self, cid, checklist_id="default", source="manual", checklist_version=None):
        self.conversation(cid)
        checklist = (self.checklist(checklist_id, checklist_version) if checklist_version is not None
                     else self.latest_checklist(checklist_id))
        eid = uuid4().hex
        answers = blank_answers(checklist)
        with self.connect() as db:
            db.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)",
                       (eid, cid, checklist_id, checklist["version"], encode(checklist),
                        encode(answers), source, stamp()))
            self._event(db, eid, "evaluation_created", source, {"checklist_version": checklist["version"]})
        return self.evaluation(eid)

    def evaluation(self, eid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM evaluations WHERE id=?", (eid,)).fetchone()
            if row is None:
                raise Invalid("Evaluation not found")
            events = db.execute("SELECT id,kind,actor,detail,created_at FROM events WHERE evaluation_id=? ORDER BY id",
                                (eid,)).fetchall()
            appeals = db.execute("SELECT * FROM appeals WHERE evaluation_id=? ORDER BY created_at", (eid,)).fetchall()
        result = dict(row)
        for key in ("checklist_snapshot", "answers"):
            result[key] = json.loads(result[key])
        result["events"] = [{**dict(event), "detail": json.loads(event["detail"])} for event in events]
        result["appeals"] = [{**dict(appeal), "decision": json.loads(appeal["decision"]) if appeal["decision"] else None}
                             for appeal in appeals]
        return result

    def evaluations_for(self, cid):
        with self.connect() as db:
            rows = db.execute("SELECT id, source, created_at, checklist_version FROM evaluations "
                              "WHERE conversation_id=? ORDER BY created_at DESC", (cid,)).fetchall()
        return [dict(row) for row in rows]

    def patch_answer(self, eid, cid, patch, actor="manual_reviewer"):
        evaluation = self.evaluation(eid)
        if cid not in evaluation["answers"]:
            raise Invalid("Criterion not found in this evaluation")
        if actor != "appeal_reviewer" and any(
            appeal["criterion_id"] == cid and appeal["status"] == "open" for appeal in evaluation["appeals"]
        ):
            raise Invalid("Resolve the open appeal before editing this criterion")
        allowed = {"applicability", "na_reason", "answer", "evidence_idx", "explanation"}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise Invalid("Unknown or empty answer fields")
        before = evaluation["answers"][cid].copy()
        after = {**before, **patch}
        if after["applicability"] == "na":
            after["answer"] = None
        evaluation["answers"][cid] = after
        with self.connect() as db:
            db.execute("UPDATE evaluations SET answers=? WHERE id=?", (encode(evaluation["answers"]), eid))
            self._event(db, eid, "answer_updated", actor, {"criterion_id": cid, "before": before, "after": after})
        return self.evaluation(eid)

    def apply_ai_draft(self, eid, response):
        evaluation = self.evaluation(eid)
        if any(appeal["status"] == "open" for appeal in evaluation["appeals"]):
            raise Invalid("Resolve open appeals before rerunning AI")
        answers = response["answers"]
        if set(answers) != set(evaluation["answers"]):
            raise Invalid("AI draft does not match checklist criteria")
        with self.connect() as db:
            db.execute("UPDATE evaluations SET answers=?, source=? WHERE id=?",
                       (encode(answers), response["model"], eid))
            self._event(db, eid, "ai_draft", response["model"],
                        {"raw": response["raw"], "usage": response["usage"], "answers": answers,
                         "provider": response["provider"],
                         "request": response["request"],
                         "cost_usd": response["cost_usd"],
                         "duration_seconds": response["duration_seconds"],
                         "prompt_version": response["prompt_version"]})
        return self.evaluation(eid)

    def log_ai_failure(self, eid, trace):
        self.evaluation(eid)
        with self.connect() as db:
            self._event(db, eid, "ai_error", trace.get("model", "provider"), trace)

    def appeal(self, eid, cid, comment):
        evaluation = self.evaluation(eid)
        if cid not in evaluation["answers"] or not str(comment).strip():
            raise Invalid("Valid criterion and comment are required")
        original_score = score(evaluation["checklist_snapshot"],
                               self.conversation(evaluation["conversation_id"]),
                               evaluation["answers"])
        aid = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO appeals VALUES (?,?,?,?,?,?,?,?)",
                       (aid, eid, cid, comment.strip(), "open", None, stamp(), None))
            self._event(db, eid, "appeal_opened", "operator",
                        {"appeal_id": aid, "criterion_id": cid, "comment": comment,
                         "original_answer": evaluation["answers"][cid],
                         "original_total": original_score["total"]})
        return self.evaluation(eid)

    def decide_appeal(self, aid, decision):
        with self.connect() as db:
            appeal = db.execute("SELECT * FROM appeals WHERE id=?", (aid,)).fetchone()
        if appeal is None or appeal["status"] != "open":
            raise Invalid("Open appeal not found")
        if decision.get("action") not in {"confirm", "change"} or not str(decision.get("reason", "")).strip():
            raise Invalid("Decision action and reason are required")
        if decision["action"] == "change":
            patch = decision.get("answer")
            if not isinstance(patch, dict):
                raise Invalid("Changed answer is required")
            evaluation = self.evaluation(appeal["evaluation_id"])
            proposed = {**evaluation["answers"],
                        appeal["criterion_id"]: {**evaluation["answers"][appeal["criterion_id"]], **patch}}
            if proposed[appeal["criterion_id"]]["applicability"] == "na":
                proposed[appeal["criterion_id"]]["answer"] = None
            score(evaluation["checklist_snapshot"], self.conversation(evaluation["conversation_id"]), proposed)
            self.patch_answer(appeal["evaluation_id"], appeal["criterion_id"], patch, "appeal_reviewer")
        with self.connect() as db:
            db.execute("UPDATE appeals SET status='decided', decision=?, decided_at=? WHERE id=?",
                       (encode(decision), stamp(), aid))
            self._event(db, appeal["evaluation_id"], "appeal_decided", "reviewer",
                        {"appeal_id": aid, **decision})
        return self.evaluation(appeal["evaluation_id"])

    @staticmethod
    def _event(db, eid, kind, actor, detail):
        db.execute("INSERT INTO events(evaluation_id,kind,actor,detail,created_at) VALUES (?,?,?,?,?)",
                   (eid, kind, actor, encode(detail), stamp()))
