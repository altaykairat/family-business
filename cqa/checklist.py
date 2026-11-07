"""S0. Checklist versions: immutable snapshots data/checklists/<id>/v<N>.json (decision 9).

Nothing is ever deleted: a new version is a new file, older versions stay as the archive. Which version is
*active* (the `HEAD`, per profile) and which versions are archived is recorded in the event log, not in the
files, so snapshots never change after they are written.

Criteria have a permanent id and revisions `c07@r1`, `c07@r2`: a revision changes when the *meaning* of the
criterion changes (any field except weight / section / status), so a weight change keeps old labels valid.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

from . import common
from .common import CQAError, load_json, log_event, now_iso, save_json, stable_hash

ANSWER_TYPES = {"binary", "scale", "flag"}
PLACEMENT_FIELDS = {"weight", "section", "enabled", "status"}


def _content(doc: dict) -> dict:
    """Checklist content without metadata — used for hashing and diffs."""
    return {k: v for k, v in doc.items() if k != "meta"}


def content_hash(doc: dict) -> str:
    return stable_hash(_content(doc))


def ids() -> list:
    folder = common.CHECKLISTS
    return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []


def versions(checklist_id: str) -> list:
    folder = common.CHECKLISTS / checklist_id
    nums = []
    for p in folder.glob("v*.json"):
        try:
            nums.append(int(p.stem[1:]))
        except ValueError:
            pass
    return sorted(nums)


def path_for(checklist_id: str, version: int) -> Path:
    return common.CHECKLISTS / checklist_id / f"v{version}.json"


def parse_ref(ref: str) -> tuple:
    """'default' → ('default', None); 'default@2' / 'default@v2' → ('default', 2)."""
    cid, _, ver = ref.partition("@")
    ver = ver.lstrip("v")
    if ver and not ver.isdigit():
        raise CQAError(f"bad checklist reference {ref!r}, expected id@N")
    return cid, int(ver) if ver else None


def make_ref(checklist_id: str, version: int) -> str:
    return f"{checklist_id}@v{version}"


# --- pointers and status (event log) ----------------------------------------

def _version_events(until: Optional[str] = None) -> list:
    from . import events

    return events.read(until=until, types={"version_activated", "version_archived", "version_restored"})


def active_version(checklist_id: str, profile: str = "default", until: Optional[str] = None) -> Optional[int]:
    """Version the HEAD pointer of `profile` points to (falls back to the 'default' profile)."""
    found = {}
    for ev in _version_events(until):
        if ev["type"] == "version_activated" and ev.get("kind") == "checklist" and ev.get("checklist_id") == checklist_id:
            found[ev.get("profile", "default")] = ev["version"]
    return found.get(profile, found.get("default"))


def status_of(checklist_id: str, version: int, until: Optional[str] = None) -> str:
    """active (HEAD) / superseded (was active) / archived / draft (never activated)."""
    status = "draft"
    ref = make_ref(checklist_id, version)
    for ev in _version_events(until):
        if ev.get("kind") != "checklist" or ev.get("ref") != ref:
            continue
        status = {"version_activated": "active", "version_archived": "archived",
                  "version_restored": "draft"}[ev["type"]]
    vs = versions(checklist_id)
    head = active_version(checklist_id, until=until) or (vs[0] if vs else None)
    if version == head and status != "archived":
        return "active"
    return "superseded" if status == "active" else status


def resolve(ref: str, until: Optional[str] = None) -> tuple:
    """Reference → (checklist_id, version). Without @N: the active version (HEAD); before any activation the
    first version — the organizers' checklist — is the HEAD. New versions are drafts until activated."""
    cid, ver = parse_ref(ref)
    vs = versions(cid)
    if not vs:
        raise CQAError(f"Checklist '{cid}' not found in {common.CHECKLISTS}", "not_found")
    if ver is None:
        ver = active_version(cid, until=until) or vs[0]
    if ver not in vs:
        raise CQAError(f"Checklist version {cid}@v{ver} not found", "not_found")
    return cid, ver


def load(ref: str) -> dict:
    """Load 'default' (the active version), 'default@2' / 'default@v2' or a path to a JSON file."""
    if ref.endswith(".json") and Path(ref).exists():
        doc = load_json(Path(ref))
    else:
        cid, ver = resolve(ref)
        doc = load_json(path_for(cid, ver))
    doc.setdefault("meta", {})
    doc["meta"]["content_hash"] = content_hash(doc)
    validate(doc)
    return doc


def ref_of(doc: dict) -> str:
    return f"{doc['meta'].get('checklist_id', 'custom')}@v{doc['meta'].get('version', 0)}"


# --- validation ----------------------------------------------------------------

def validate(doc: dict) -> None:
    sections = {s["id"] for s in doc.get("sections", [])}
    _validate_clarifications(doc)
    seen = set()
    for c in doc.get("criteria", []):
        cid = c.get("id")
        if not cid or cid in seen:
            raise CQAError(f"Criterion id missing or duplicated: {cid!r}")
        seen.add(cid)
        if c.get("type") not in ANSWER_TYPES:
            raise CQAError(f"{cid}: unknown type {c.get('type')!r}, expected one of {sorted(ANSWER_TYPES)}")
        if c["type"] == "scale":
            if not c.get("scale"):
                raise CQAError(f"{cid}: scale criterion without scale steps")
            for s in c["scale"]:
                if not isinstance(s.get("value"), (int, float)):
                    raise CQAError(f"{cid}: scale step value must be a number, got {s.get('value')!r}")
        w = c.get("weight", 0)
        if not isinstance(w, (int, float)) or w < 0:
            raise CQAError(f"{cid}: weight must be a non-negative number, got {w!r}")
        if c.get("section") and sections and c["section"] not in sections:
            raise CQAError(f"{cid}: unknown section {c['section']!r}")


def _validate_clarifications(doc: dict) -> None:
    crit_ids = {c.get("id") for c in doc.get("criteria", [])}
    seen = set()
    for u in doc.get("clarifications", []) or []:
        uid = u.get("id")
        if not uid or uid in seen:
            raise CQAError(f"Clarification id missing or duplicated: {uid!r}")
        seen.add(uid)
        if not (u.get("text") or "").strip():
            raise CQAError(f"{uid}: clarification text is empty")
        if u.get("criterion_id") and u["criterion_id"] not in crit_ids:
            raise CQAError(f"{uid}: unknown criterion {u['criterion_id']!r}")


def active_clarifications(doc: dict, criterion_ids: Optional[list] = None) -> list:
    """Rule clarifications of a version that take part in prompts (sprint 1, item 4). A clarification bound to a
    criterion goes only into prompts that evaluate that criterion; one without criterion_id goes everywhere."""
    out = []
    for u in doc.get("clarifications", []) or []:
        if u.get("status", "active") == "archived":
            continue
        if criterion_ids is not None and u.get("criterion_id") and u["criterion_id"] not in criterion_ids:
            continue
        out.append(u)
    return out


def clarification_hash(u: dict) -> str:
    return stable_hash({k: u.get(k) for k in ("text", "reason", "criterion_id")}, 10)


def clarification_revs(doc: dict) -> dict:
    """{'u1': 'u1@r2'} — revision numbers follow the content across all versions of the checklist."""
    cid, ver = doc["meta"].get("checklist_id"), doc["meta"].get("version")
    table: dict = {}
    for v in (versions(cid) if cid and ver else []):
        for u in load_json(path_for(cid, v)).get("clarifications", []) or []:
            revs = table.setdefault(u["id"], [])
            h = clarification_hash(u)
            if h not in revs:
                revs.append(h)
    out = {}
    for u in active_clarifications(doc):
        h = clarification_hash(u)
        revs = table.get(u["id"], [])
        out[u["id"]] = f"{u['id']}@r{revs.index(h) + 1}" if h in revs else f"{u['id']}@{h}"
    return out


def _next_clarification_id(doc: dict) -> str:
    nums = [int(u["id"][1:]) for u in doc.get("clarifications", []) or [] if str(u.get("id", "")).startswith("u")
            and u["id"][1:].isdigit()]
    return f"u{max(nums, default=0) + 1}"


def active_criteria(doc: dict) -> list:
    """Criteria that take part in evaluation (status 'archived' / enabled=false are skipped)."""
    return [c for c in doc["criteria"] if c.get("enabled", True) and c.get("status", "active") != "archived"]


def criterion(doc: dict, criterion_id: str) -> dict:
    for c in doc["criteria"]:
        if c["id"] == criterion_id:
            return c
    raise CQAError(f"criterion {criterion_id} not in {ref_of(doc)}", "not_found")


def section_title(doc: dict, section_id: Optional[str]) -> str:
    for s in doc.get("sections", []):
        if s["id"] == section_id:
            return s["title"]
    return section_id or ""


# --- revisions -----------------------------------------------------------------

def semantic_hash(c: dict) -> str:
    return stable_hash({k: v for k, v in c.items() if k not in PLACEMENT_FIELDS}, 10)


def revisions(checklist_id: str) -> dict:
    """{criterion_id: [{'rev': 1, 'hash': …, 'versions': [1, 2]}, …]} across all versions of a checklist."""
    out: dict = {}
    for v in versions(checklist_id):
        doc = load_json(path_for(checklist_id, v))
        for c in doc["criteria"]:
            revs = out.setdefault(c["id"], [])
            h = semantic_hash(c)
            hit = next((r for r in revs if r["hash"] == h), None)
            if hit is None:
                hit = {"rev": len(revs) + 1, "hash": h, "versions": [], "title": c.get("title")}
                revs.append(hit)
            hit["versions"].append(v)
    return out


def criterion_revs(doc: dict) -> dict:
    """{criterion_id: 'c07@r2'} for a loaded version (custom documents get hash-based revisions)."""
    cid, ver = doc["meta"].get("checklist_id"), doc["meta"].get("version")
    table = revisions(cid) if cid and ver else {}
    out = {}
    for c in doc["criteria"]:
        h = semantic_hash(c)
        rev = next((r["rev"] for r in table.get(c["id"], []) if r["hash"] == h), None)
        out[c["id"]] = f"{c['id']}@r{rev}" if rev else f"{c['id']}@{h}"
    return out


# --- saving --------------------------------------------------------------------

def save_doc(doc: dict, checklist_id: str, author: str, reason: str, parent: Optional[int] = None,
             source: Optional[dict] = None) -> dict:
    """Create the next immutable version from a document. Returns {'ref', 'path', 'version', 'content_hash'}."""
    doc = copy.deepcopy(doc)
    validate(doc)
    vs = versions(checklist_id)
    new_version = (vs[-1] + 1) if vs else 1
    if parent is None and vs:
        parent = vs[-1]
    parent_doc = load(f"{checklist_id}@{parent}") if parent else None
    h = content_hash(doc)
    for v in vs:
        if content_hash(load_json(path_for(checklist_id, v))) == h:
            raise CQAError(f"No changes: identical to {checklist_id}@v{v} — nothing saved.", "conflict")
    doc["meta"] = {
        "checklist_id": checklist_id,
        "version": new_version,
        "created_at": now_iso(),
        "author": author,
        "reason": reason,
        "parent_version": parent,
        "parent_hash": parent_doc["meta"]["content_hash"] if parent_doc else None,
        "source": source,
    }
    out = path_for(checklist_id, new_version)
    save_json(out, doc)
    ref = make_ref(checklist_id, new_version)
    log_event("checklist_version_created", actor=author, checklist=ref, checklist_id=checklist_id,
              version=new_version, parent=parent, author=author, reason=reason, content_hash=h,
              source=source, diff=diff(parent_doc, load(ref)) if parent_doc else ["(первая версия)"])
    return {"ref": ref, "path": common.rel(out), "version": new_version, "content_hash": h}


def save_new_version(source: Path, checklist_id: str, author: str, reason: str) -> Path:
    """Create the next immutable version from an edited JSON file (CLI `checklist save`)."""
    res = save_doc(load_json(source), checklist_id, author, reason)
    return path_for(checklist_id, res["version"])


# --- edit operations (used by humans through the API and by accepted proposals) ----

def apply_ops(doc: dict, ops: list) -> dict:
    """Apply structured edit operations to a copy of `doc`. Never touches the stored version.

    ops: {"op": "set", "path": "criteria.c07.na_condition" | "title" | "scoring.rounding", "value": …}
         {"op": "set_weight", "criterion_id": "c07", "value": 3}
         {"op": "add_note", "criterion_id": "c07", "value": "уточнение"}
         {"op": "remove_note", "criterion_id": "c07", "index": 0}
         {"op": "add_global_rule", "value": "…", "index": optional position}
         {"op": "edit_global_rule", "index": 2, "value": "…"}
         {"op": "remove_global_rule", "index": 2}
         {"op": "add_criterion", "value": {criterion}, "after": optional id}
         {"op": "archive_criterion", "criterion_id": "c03"}      (kept in the snapshot with status archived)
         {"op": "restore_criterion", "criterion_id": "c03"}
         {"op": "replace_criterion", "criterion_id": "c03", "value": {new criterion with a NEW id}}
         {"op": "add_section", "value": {"id": …, "title": …}}
         {"op": "add_clarification", "value": {"text": "…", "reason": "…", "criterion_id": "c07" | null,
                                               "source": {"proposal_id": …, "decisions": […]}}}
         {"op": "edit_clarification", "id": "u1", "value": {"text"?: …, "reason"?: …, "criterion_id"?: …}}
         {"op": "archive_clarification", "id": "u1"}   /   {"op": "restore_clarification", "id": "u1"}
    Clarifications are rendered as the «Уточнения правил» section of the prompt ({{RULES}}); ids are never reused.
    """
    new = copy.deepcopy(_content(doc))
    new["meta"] = copy.deepcopy(doc.get("meta", {}))
    rules = new.setdefault("global_rules", [])
    for n, op in enumerate(ops, 1):
        kind = op.get("op")
        try:
            if kind == "set":
                _set_path(new, op["path"], op["value"])
            elif kind == "set_weight":
                criterion(new, op["criterion_id"])["weight"] = op["value"]
            elif kind == "add_note":
                criterion(new, op["criterion_id"]).setdefault("notes", []).append(op["value"])
            elif kind == "remove_note":
                criterion(new, op["criterion_id"]).setdefault("notes", []).pop(int(op["index"]))
            elif kind == "add_global_rule":
                rules.insert(int(op["index"]), op["value"]) if "index" in op else rules.append(op["value"])
            elif kind == "edit_global_rule":
                rules[int(op["index"])] = op["value"]
            elif kind == "remove_global_rule":
                rules.pop(int(op["index"]))
            elif kind == "add_criterion":
                c = copy.deepcopy(op["value"])
                if any(x["id"] == c.get("id") for x in new["criteria"]):
                    raise CQAError(f"criterion id {c.get('id')} already exists (ids are never reused)")
                pos = len(new["criteria"])
                if op.get("after"):
                    pos = [x["id"] for x in new["criteria"]].index(op["after"]) + 1
                new["criteria"].insert(pos, c)
            elif kind == "archive_criterion":
                criterion(new, op["criterion_id"])["status"] = "archived"
            elif kind == "restore_criterion":
                criterion(new, op["criterion_id"]).pop("status", None)
            elif kind == "replace_criterion":
                old = criterion(new, op["criterion_id"])
                c = copy.deepcopy(op["value"])
                if c.get("id") == old["id"] or any(x["id"] == c.get("id") for x in new["criteria"]):
                    raise CQAError("a replaced criterion needs a new, unused id (CH.4)")
                old["status"] = "archived"
                old["replaced_by"] = c["id"]
                c.setdefault("replaces", old["id"])
                pos = [x["id"] for x in new["criteria"]].index(old["id"]) + 1
                new["criteria"].insert(pos, c)
            elif kind == "add_section":
                new.setdefault("sections", []).append(copy.deepcopy(op["value"]))
            elif kind == "add_clarification":
                u = {k: v for k, v in copy.deepcopy(op["value"]).items() if k in ("text", "reason", "criterion_id",
                                                                                  "source")}
                u = {"id": _next_clarification_id(new), **u}
                new.setdefault("clarifications", []).append(u)
            elif kind in ("edit_clarification", "archive_clarification", "restore_clarification"):
                u = next((x for x in new.get("clarifications", []) or [] if x["id"] == op["id"]), None)
                if u is None:
                    raise CQAError(f"clarification {op['id']} not in this version")
                if kind == "edit_clarification":
                    u.update({k: v for k, v in op["value"].items() if k in ("text", "reason", "criterion_id",
                                                                            "source")})
                elif kind == "archive_clarification":
                    u["status"] = "archived"
                else:
                    u.pop("status", None)
            else:
                raise CQAError(f"unknown op {kind!r}")
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise CQAError(f"operation #{n} {op}: {e}")
    validate(new)
    new["meta"]["content_hash"] = content_hash(new)
    return new


def _set_path(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    if parts[0] == "criteria" and len(parts) >= 3:
        node = criterion(doc, parts[1])
        parts = parts[2:]
    else:
        node = doc
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


# --- diff ------------------------------------------------------------------------

def diff_struct(a: dict, b: dict) -> dict:
    """Structured difference: added / removed / archived / changed criteria and changed top-level parts."""
    ca = {c["id"]: c for c in a["criteria"]}
    cb = {c["id"]: c for c in b["criteria"]}
    changed = {}
    for cid in sorted(set(ca) & set(cb)):
        fields = {k: {"from": ca[cid].get(k), "to": cb[cid].get(k)}
                  for k in sorted(set(ca[cid]) | set(cb[cid])) if ca[cid].get(k) != cb[cid].get(k)}
        if fields:
            changed[cid] = fields
    ra = a.get("global_rules", [])
    rb = b.get("global_rules", [])
    return {
        "added": sorted(set(cb) - set(ca)),
        "removed": sorted(set(ca) - set(cb)),
        "archived": sorted(c for c in set(ca) & set(cb)
                           if cb[c].get("status") == "archived" and ca[c].get("status") != "archived"),
        "changed": changed,
        "global_rules": {"added": [r for r in rb if r not in ra], "removed": [r for r in ra if r not in rb]},
        "clarifications": _diff_clarifications(a, b),
        "top_level": sorted(k for k in ("sections", "scoring", "title") if a.get(k) != b.get(k)),
    }


def _diff_clarifications(a: dict, b: dict) -> dict:
    ua = {u["id"]: u for u in active_clarifications(a)}
    ub = {u["id"]: u for u in active_clarifications(b)}
    return {"added": sorted(set(ub) - set(ua)), "removed": sorted(set(ua) - set(ub)),
            "changed": sorted(i for i in set(ua) & set(ub) if clarification_hash(ua[i]) != clarification_hash(ub[i])),
            "criteria": sorted({(ub.get(i) or ua.get(i)).get("criterion_id") or "*"
                                for i in (set(ua) ^ set(ub)) | {i for i in set(ua) & set(ub)
                                                                 if clarification_hash(ua[i]) != clarification_hash(ub[i])}})}


def diff(a: dict, b: dict) -> list:
    """Human-readable list of differences between two checklist versions."""
    d = diff_struct(a, b)
    cb = {c["id"]: c for c in b["criteria"]}
    ca = {c["id"]: c for c in a["criteria"]}
    lines = [f"+ добавлен {cid} «{cb[cid].get('title')}»" for cid in d["added"]]
    lines += [f"- удалён {cid} «{ca[cid].get('title')}»" for cid in d["removed"]]
    for cid, fields in d["changed"].items():
        for key, ch in fields.items():
            lines.append(f"~ {cid}.{key}: {ch['from']!r} → {ch['to']!r}")
    lines += [f"+ правило: {r}" for r in d["global_rules"]["added"]]
    lines += [f"- правило: {r}" for r in d["global_rules"]["removed"]]
    ub = {u["id"]: u for u in b.get("clarifications", []) or []}
    ua = {u["id"]: u for u in a.get("clarifications", []) or []}
    lines += [f"+ уточнение {i} [{ub[i].get('criterion_id') or 'все'}]: {ub[i]['text']}" for i in d["clarifications"]["added"]]
    lines += [f"- уточнение {i}: {ua[i]['text']}" for i in d["clarifications"]["removed"]]
    lines += [f"~ уточнение {i}: {ua[i]['text']!r} → {ub[i]['text']!r}" for i in d["clarifications"]["changed"]]
    lines += [f"~ {key} изменён" for key in d["top_level"]]
    return lines or ["(различий нет)"]
