"""People directory (decisions 10 and 20; PE.*). The data has no operator field, so operators are suggested by
the name the operator introduces themselves with (F.3) and confirmed by a human («Подтвердить оператора»).

Demo directory (`seed_demo`): op-01…op-NN by operator names in chats, op-c1…op-c5 for calls (names are
masked), reviewers r-01…r-03, admin a-01. Marked `demo: true` — they are demonstration data, not real people.
"""

from __future__ import annotations

from typing import Optional

from . import access, events, state
from . import conversation as cv
from .common import CQAError


def register(person_id: str, name: str, role: str, actor: str, team: Optional[str] = None,
             languages: Optional[list] = None, demo: bool = False) -> dict:
    st = state.load()
    if st.people:  # the very first registrations bootstrap the directory
        access.require(actor, "people.manage", st)
    if role not in ("operator", "reviewer", "admin", "supervisor"):
        raise CQAError("role must be operator, reviewer, admin or supervisor")
    if person_id in st.people:
        raise CQAError(f"{person_id} already exists", "conflict")
    events.append("person_registered", actor=actor, person_id=person_id, name=name, role=role, team=team,
                  languages=languages or [], status="active", demo=demo)
    return {"person_id": person_id, "role": role}


def update(person_id: str, fields: dict, actor: str) -> dict:
    st = state.load()
    access.require(actor, "people.manage", st)
    if person_id not in st.people:
        raise CQAError(f"{person_id} not found", "not_found")
    allowed = {"name", "team", "languages", "status"}
    bad = set(fields) - allowed
    if bad:
        raise CQAError(f"cannot change {sorted(bad)}; allowed: {sorted(allowed)}")
    events.append("person_updated", actor=actor, person_id=person_id, fields=fields)
    return {"person_id": person_id, **fields}


def seed_demo(dataset: str = "dev", actor: str = "a-01", reviewers: int = 3) -> dict:
    """Create the demo directory and *suggest* (not assign) an operator for every conversation."""
    st = state.load()
    if st.people:
        raise CQAError("the directory is not empty — demo seed only runs once", "conflict")
    register("a-01", "Админ (демо)", "admin", actor, demo=True)
    for i in range(1, reviewers + 1):
        register(f"r-{i:02d}", f"Проверяющий {i} (демо)", "reviewer", actor, demo=True)
    chats, calls = 0, 0
    for conv_id in cv.list_ids(dataset):
        conv = cv.load(dataset, conv_id)
        if conv["is_call"]:
            calls += 1
            pid, name, method = f"op-c{calls}", "[Имя скрыто]", "call: name masked, one id per call"
        else:
            chats += 1
            name = cv.guess_operator_name(conv)
            pid, method = f"op-{chats:02d}", "name from the first operator reply (F.3)" if name else "no introduction"
            name = name or "(не представился)"
        register(pid, name, "operator", actor, team="demo", languages=[conv["language"]], demo=True)
        events.append("operator_suggested", actor="system", conversation_id=conv_id, operator_id=pid, name=name,
                      method=method)
    return {"admins": 1, "reviewers": reviewers, "operators": chats + calls,
            "note": "operators are suggested; confirm with people.assign"}


def assign(conversation_id: str, operator_id: str, actor: str) -> dict:
    st = state.load()
    access.require(actor, "people.manage", st)
    if st.role_of(operator_id) != "operator":
        raise CQAError(f"{operator_id} is not an operator")
    events.append("operator_assigned", actor=actor, conversation_id=conversation_id, operator_id=operator_id,
                  suggested=(st.suggested_operator.get(conversation_id) or {}).get("operator_id") == operator_id,
                  previous=st.operator_of.get(conversation_id))
    return {"conversation_id": conversation_id, "operator_id": operator_id}


def confirm_suggestions(actor: str, conversation_ids: Optional[list] = None) -> dict:
    """Bulk button: confirm the suggested operator for the listed (or all unassigned) conversations."""
    st = state.load()
    access.require(actor, "people.manage", st)
    done = []
    for conv_id, sug in st.suggested_operator.items():
        if conversation_ids and conv_id not in conversation_ids:
            continue
        if conv_id in st.operator_of:
            continue
        events.append("operator_assigned", actor=actor, conversation_id=conv_id, operator_id=sug["operator_id"],
                      suggested=True, previous=None)
        done.append(conv_id)
    return {"confirmed": len(done)}


def directory(role: Optional[str] = None, until: Optional[str] = None) -> list:
    st = state.load(until)
    return [p for p in st.people.values() if role is None or p["role"] == role]
