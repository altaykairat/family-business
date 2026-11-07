"""Analytics over people and relations (decision 10; SO.*, SR.*, RL.*, SP.*). Every figure comes with its
sample size n and, for shares, a 95% Wilson interval. Comparisons with the team are shown only from n ≥ min.
No automatic labels such as «проблемный оператор» (SP.4): the system shows numbers, people draw conclusions.

Operator figures use *published* evaluations (the official ones the operator sees), with reviewer decisions
applied on top (final answers).
"""

from __future__ import annotations

from typing import Optional

from . import db, stats
from .common import setting


def _z() -> float:
    return float(setting("analytics.ci_z", 1.96))


def _p(k, n) -> dict:
    return stats.proportion(int(k or 0), int(n or 0), _z())


def operators(until: Optional[str] = None) -> list:
    conn = db.connect(until)
    min_n = int(setting("analytics.min_n_for_comparison", 10))
    team = conn.execute("SELECT AVG(final_total) m, COUNT(*) n FROM fact_evaluation WHERE published=1").fetchone()
    out = []
    for op in conn.execute("SELECT person_id, name FROM dim_person WHERE role='operator' ORDER BY person_id"):
        pid = op["person_id"]
        ev = conn.execute("""SELECT COUNT(*) n, AVG(e.final_total) mean_total, SUM(e.gross_final) gross
            FROM fact_evaluation e JOIN dim_conversation c USING (conversation_id)
            WHERE e.published=1 AND c.operator_id=?""", (pid,)).fetchone()
        ap = conn.execute("""SELECT COUNT(*) n, SUM(status='decided') decided FROM fact_appeal WHERE operator_id=?""",
                          (pid,)).fetchone()
        dec = conn.execute("""SELECT COUNT(*) n, SUM(changed) changed, SUM(type='A') a, SUM(type='B') b,
            SUM(type='C') c, SUM(type='D') d FROM fact_decision WHERE operator_id=? AND appeal_id IS NOT NULL""",
                           (pid,)).fetchone()
        by_crit = [dict(r) for r in conn.execute("""SELECT f.criterion_id, COUNT(*) n,
                SUM(f.final_answer='no') no_count, AVG(f.final_points) mean_points
            FROM fact_criterion f JOIN dim_conversation c USING (conversation_id)
            WHERE f.published=1 AND c.operator_id=? GROUP BY f.criterion_id""", (pid,))]
        appeals_by_crit = {r["criterion_id"]: r["n"] for r in conn.execute(
            "SELECT criterion_id, COUNT(*) n FROM fact_appeal WHERE operator_id=? GROUP BY criterion_id", (pid,))}
        n = ev["n"] or 0
        out.append({
            "operator_id": pid, "name": op["name"],
            "evaluations": n, "mean_total": ev["mean_total"] and round(ev["mean_total"], 2),
            "gross_errors": ev["gross"] or 0,
            "appeals": ap["n"], "appeal_rate": _p(ap["n"], n),
            "appeal_success": _p(dec["changed"], dec["n"]),
            "appeal_types": {"A": dec["a"] or 0, "B": dec["b"] or 0, "C": dec["c"] or 0, "D": dec["d"] or 0},
            "appeals_by_criterion": appeals_by_crit,
            "by_criterion": [{**r, "no_share": _p(r["no_count"], r["n"]),
                              "mean_points": r["mean_points"] and round(r["mean_points"], 2)} for r in by_crit],
            "vs_team": ({"team_mean": round(team["m"], 2), "delta": round(ev["mean_total"] - team["m"], 2)}
                        if n >= min_n and team["m"] is not None and ev["mean_total"] is not None
                        else f"сравнение показывается при n ≥ {min_n}"),
        })
    conn.close()
    return out


def reviewers(until: Optional[str] = None) -> list:
    conn = db.connect(until)
    out = []
    ids = [r[0] for r in conn.execute("""SELECT person_id FROM dim_person WHERE role IN ('reviewer','admin')
                                         UNION SELECT DISTINCT reviewer_id FROM fact_decision""")]
    for rid in sorted(set(ids)):
        r = conn.execute("""SELECT COUNT(*) n, AVG(time_spent_s) t, SUM(changed) changed,
            SUM(total_delta > 0) up, SUM(total_delta < 0) down, AVG(total_delta) mean_delta,
            SUM(type='A') a, SUM(type='B') b, SUM(type='C') c, SUM(type='D') d,
            SUM(saw_counterargument) saw, SUM(saw_counterargument AND changed) saw_changed,
            SUM(blind) blind_n, SUM(blind AND changed) blind_changed,
            SUM(NOT blind) normal_n, SUM((NOT blind) AND changed) normal_changed
            FROM fact_decision WHERE reviewer_id=?""", (rid,)).fetchone()
        if not r["n"]:
            continue
        by_crit = {x["criterion_id"]: {"n": x["n"], "changed": _p(x["ch"], x["n"])} for x in conn.execute(
            "SELECT criterion_id, COUNT(*) n, SUM(changed) ch FROM fact_decision WHERE reviewer_id=? GROUP BY 1", (rid,))}
        out.append({
            "reviewer_id": rid, "decisions": r["n"], "mean_time_s": r["t"] and round(r["t"], 1),
            "change_share": _p(r["changed"], r["n"]), "agreement_with_model": _p(r["n"] - (r["changed"] or 0), r["n"]),
            "direction": {"up": r["up"] or 0, "down": r["down"] or 0,
                          "mean_total_delta": r["mean_delta"] and round(r["mean_delta"], 2)},
            "types": {"A": r["a"] or 0, "B": r["b"] or 0, "C": r["c"] or 0, "D": r["d"] or 0},
            "by_criterion": by_crit,
            "counterargument": {"seen": r["saw"] or 0, "changed_after_seeing": _p(r["saw_changed"], r["saw"])},
            "blind_vs_normal": {"blind": _p(r["blind_changed"], r["blind_n"]),
                                "normal": _p(r["normal_changed"], r["normal_n"])},
        })
    conn.close()
    return out


def pairs(until: Optional[str] = None) -> list:
    """RL.1 operator × reviewer: appeals decided and share changed, next to the reviewer's overall share."""
    conn = db.connect(until)
    overall = {r["reviewer_id"]: _p(r["ch"], r["n"]) for r in conn.execute(
        "SELECT reviewer_id, COUNT(*) n, SUM(changed) ch FROM fact_decision WHERE appeal_id IS NOT NULL GROUP BY 1")}
    rows = [{"operator_id": r["operator_id"], "reviewer_id": r["reviewer_id"], "decisions": r["n"],
             "changed": _p(r["ch"], r["n"]), "reviewer_overall": overall.get(r["reviewer_id"])}
            for r in conn.execute("""SELECT operator_id, reviewer_id, COUNT(*) n, SUM(changed) ch FROM fact_decision
                                     WHERE appeal_id IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""")]
    conn.close()
    return rows


def criteria(until: Optional[str] = None) -> list:
    """RL.3 criterion × everything: answers distribution, appeals, changes, types."""
    conn = db.connect(until)
    rows = []
    for r in conn.execute("""SELECT criterion_id, COUNT(*) n, SUM(original_answer='yes') yes,
            SUM(original_answer='no') no, SUM(original_answer='na') na, AVG(original_points) pts
            FROM fact_criterion WHERE current=1 GROUP BY 1 ORDER BY 1"""):
        a = conn.execute("SELECT COUNT(*) n FROM fact_appeal WHERE criterion_id=?", (r["criterion_id"],)).fetchone()
        d = conn.execute("""SELECT COUNT(*) n, SUM(changed) ch, SUM(type='A') a, SUM(type='B') b, SUM(type='C') c,
            SUM(type='D') dd FROM fact_decision WHERE criterion_id=?""", (r["criterion_id"],)).fetchone()
        rows.append({"criterion_id": r["criterion_id"], "evaluations": r["n"],
                     "answers": {"yes": r["yes"] or 0, "no": r["no"] or 0, "na": r["na"] or 0},
                     "mean_points": r["pts"] and round(r["pts"], 2),
                     "appeals": a["n"], "appeal_rate": _p(a["n"], r["n"]), "decisions": d["n"],
                     "changed": _p(d["ch"], d["n"]),
                     "types": {"A": d["a"] or 0, "B": d["b"] or 0, "C": d["c"] or 0, "D": d["dd"] or 0}})
    conn.close()
    return rows


def model_errors(by: str = "language", until: Optional[str] = None) -> list:
    """RL.2 where the model is corrected by reviewers: agent × language / channel / ending / criterion."""
    dims = {"language": "c.language", "channel": "c.channel", "ending": "c.ending", "criterion": "d.criterion_id",
            "agent": "d.agent"}
    if by not in dims:
        raise ValueError(f"by must be one of {sorted(dims)}")
    conn = db.connect(until)
    rows = [{"agent": r["agent"], by: r["k"], "decisions": r["n"], "model_corrected": _p(r["ch"], r["n"])}
            for r in conn.execute(f"""SELECT d.agent agent, {dims[by]} k, COUNT(*) n, SUM(d.changed) ch
                FROM fact_decision d LEFT JOIN dim_conversation c USING (conversation_id)
                GROUP BY 1, 2 ORDER BY 1, 2""")]
    conn.close()
    return rows


VARIABLES = {
    "mean_total": "AVG(e.final_total)",
    "evaluations": "COUNT(DISTINCT e.evaluation_id)",
    "gross_errors": "SUM(e.gross_final)",
}


def relate(x: str, y: str, until: Optional[str] = None) -> dict:
    """RL.4: two per-operator variables → points + Pearson r (with n). Variables: mean_total, evaluations,
    gross_errors, appeals, appeal_rate, appeal_success."""
    ops = {o["operator_id"]: o for o in operators(until)}

    def val(o, v):
        if v in ("appeal_rate", "appeal_success"):
            return o[v]["share"]
        return {"mean_total": o["mean_total"], "evaluations": o["evaluations"], "gross_errors": o["gross_errors"],
                "appeals": o["appeals"]}.get(v)

    allowed = {"mean_total", "evaluations", "gross_errors", "appeals", "appeal_rate", "appeal_success"}
    if x not in allowed or y not in allowed:
        raise ValueError(f"variables: {sorted(allowed)}")
    pts = [{"operator_id": k, "x": val(o, x), "y": val(o, y)} for k, o in ops.items()]
    pts = [p for p in pts if p["x"] is not None and p["y"] is not None]
    return {"x": x, "y": y, "n": len(pts), "pearson_r": stats.pearson([p["x"] for p in pts], [p["y"] for p in pts]),
            "points": pts, "note": "корреляция ≠ причинность; при малом n — шум"}


def overview(until: Optional[str] = None) -> dict:
    conn = db.connect(until)
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out = {"conversations": one("SELECT COUNT(*) FROM dim_conversation"),
           "evaluations": one("SELECT COUNT(*) FROM fact_evaluation WHERE current=1"),
           "published": one("SELECT COUNT(*) FROM fact_evaluation WHERE published=1"),
           "mean_final_total_published": one("SELECT ROUND(AVG(final_total),2) FROM fact_evaluation WHERE published=1"),
           "gross_errors_published": one("SELECT SUM(gross_final) FROM fact_evaluation WHERE published=1"),
           "appeals": dict(conn.execute("SELECT status, COUNT(*) FROM fact_appeal GROUP BY 1").fetchall()),
           "decision_types": dict(conn.execute("SELECT type, COUNT(*) FROM fact_decision GROUP BY 1").fetchall()),
           "proposals": dict(conn.execute("SELECT status, COUNT(*) FROM fact_proposal GROUP BY 1").fetchall()),
           "precedents": one("SELECT COUNT(*) FROM fact_decision WHERE precedent=1"),
           "by_language": [dict(r) for r in conn.execute("""SELECT c.language, COUNT(*) n,
                ROUND(AVG(e.final_total),2) mean_total FROM fact_evaluation e JOIN dim_conversation c
                USING (conversation_id) WHERE e.published=1 GROUP BY 1""")]}
    conn.close()
    return out
