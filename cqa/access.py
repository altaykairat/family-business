"""Who may do what (decision 7, table «Права» in discussion.md). No authentication in the prototype: the actor
id is passed explicitly and its role comes from the people directory (or from the id prefix: op-, r-, a-, s-).

The pipeline (LLM, orchestrator, `system`) may only draft evaluations, propose and give counter-arguments:
it can never decide an appeal, change the checklist, weights, prompts or parameters, or activate a version.
"""

from __future__ import annotations

from typing import Optional

from . import state
from .common import CQAError

ANY = {"operator", "reviewer", "admin", "supervisor", "pipeline", "unknown"}
HUMANS = {"operator", "reviewer", "admin", "supervisor"}
STAFF = {"reviewer", "admin", "supervisor"}

RULES = {
    "appeal.file": {"operator", "supervisor", "admin"},
    "appeal.assign": STAFF,
    "appeal.open": STAFF,
    "appeal.close": {"admin", "supervisor"},
    "decision.make": {"reviewer", "admin"},
    "precedent.mark": {"reviewer", "admin"},
    "reviewer.settings": {"reviewer", "admin"},
    "model.reveal": STAFF,
    "evaluation.run": STAFF,
    "evaluation.publish": {"admin", "supervisor"},
    "task.create": STAFF,
    "task.respond": STAFF,
    "proposal.create": STAFF | {"pipeline"},
    "proposal.resolve": {"admin"},
    "checklist.change": {"admin"},
    "version.activate": {"admin"},
    "params.change": {"admin"},
    "people.manage": {"admin", "supervisor"},
    "gt.manage": {"admin", "reviewer"},
    "dataset.manage": {"admin", "supervisor"},
    "metrics.run": STAFF,
    "export": STAFF,
}


def require(actor: Optional[str], action: str, st: Optional[state.State] = None) -> str:
    st = st or state.load()
    role = st.role_of(actor)
    allowed = RULES.get(action, ANY)
    if role not in allowed:
        raise CQAError(f"{actor or 'anonymous'} ({role}) may not {action}; allowed roles: {sorted(allowed)}",
                       "forbidden")
    return role
