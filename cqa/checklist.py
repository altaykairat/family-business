"""S0. Checklist versions: immutable snapshots data/checklists/<id>/v<N>.json.

Nothing is ever deleted: a new version is a new file, older versions stay as the archive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .common import CHECKLISTS, load_json, log_event, now_iso, save_json, stable_hash

ANSWER_TYPES = {"binary", "scale", "flag"}


def _content(doc: dict) -> dict:
    """Checklist content without metadata — used for hashing and diffs."""
    return {k: v for k, v in doc.items() if k != "meta"}


def versions(checklist_id: str) -> list:
    folder = CHECKLISTS / checklist_id
    nums = []
    for p in folder.glob("v*.json"):
        try:
            nums.append(int(p.stem[1:]))
        except ValueError:
            pass
    return sorted(nums)


def path_for(checklist_id: str, version: int) -> Path:
    return CHECKLISTS / checklist_id / f"v{version}.json"


def load(ref: str) -> dict:
    """Load 'default', 'default@2' or a path. Without @N the latest version is used."""
    if ref.endswith(".json") and Path(ref).exists():
        doc = load_json(Path(ref))
    else:
        cid, _, ver = ref.partition("@")
        vs = versions(cid)
        if not vs:
            raise SystemExit(f"Checklist '{cid}' not found in {CHECKLISTS}")
        doc = load_json(path_for(cid, int(ver) if ver else vs[-1]))
    doc.setdefault("meta", {})
    doc["meta"]["content_hash"] = stable_hash(_content(doc))
    validate(doc)
    return doc


def ref_of(doc: dict) -> str:
    return f"{doc['meta'].get('checklist_id', 'custom')}@v{doc['meta'].get('version', 0)}"


def validate(doc: dict) -> None:
    sections = {s["id"] for s in doc.get("sections", [])}
    seen = set()
    for c in doc.get("criteria", []):
        cid = c.get("id")
        if not cid or cid in seen:
            raise SystemExit(f"Criterion id missing or duplicated: {cid!r}")
        seen.add(cid)
        if c.get("type") not in ANSWER_TYPES:
            raise SystemExit(f"{cid}: unknown type {c.get('type')!r}, expected one of {sorted(ANSWER_TYPES)}")
        if c["type"] == "scale" and not c.get("scale"):
            raise SystemExit(f"{cid}: scale criterion without scale steps")
        if c.get("section") and sections and c["section"] not in sections:
            raise SystemExit(f"{cid}: unknown section {c['section']!r}")


def active_criteria(doc: dict) -> list:
    """Criteria that take part in evaluation (status 'archived' / enabled=false are skipped)."""
    return [c for c in doc["criteria"] if c.get("enabled", True) and c.get("status", "active") != "archived"]


def section_title(doc: dict, section_id: Optional[str]) -> str:
    for s in doc.get("sections", []):
        if s["id"] == section_id:
            return s["title"]
    return section_id or ""


def save_new_version(source: Path, checklist_id: str, author: str, reason: str) -> Path:
    """Create the next immutable version from an edited JSON file."""
    doc = load_json(source)
    vs = versions(checklist_id)
    new_version = (vs[-1] + 1) if vs else 1
    parent = load(f"{checklist_id}@{vs[-1]}") if vs else None
    doc["meta"] = {
        "checklist_id": checklist_id,
        "version": new_version,
        "created_at": now_iso(),
        "author": author,
        "reason": reason,
        "parent_version": vs[-1] if vs else None,
        "parent_hash": parent["meta"]["content_hash"] if parent else None,
        "status": "active",
    }
    validate(doc)
    if parent and stable_hash(_content(doc)) == parent["meta"]["content_hash"]:
        raise SystemExit("No changes compared to the latest version — nothing saved.")
    out = path_for(checklist_id, new_version)
    save_json(out, doc)
    log_event("checklist_version_created", checklist=f"{checklist_id}@v{new_version}",
              parent=doc["meta"]["parent_version"], author=author, reason=reason,
              content_hash=stable_hash(_content(doc)))
    return out


def diff(a: dict, b: dict) -> list:
    """Human-readable list of differences between two checklist versions."""
    lines = []
    ca = {c["id"]: c for c in a["criteria"]}
    cb = {c["id"]: c for c in b["criteria"]}
    for cid in sorted(set(cb) - set(ca)):
        lines.append(f"+ добавлен {cid} «{cb[cid].get('title')}»")
    for cid in sorted(set(ca) - set(cb)):
        lines.append(f"- удалён {cid} «{ca[cid].get('title')}»")
    for cid in sorted(set(ca) & set(cb)):
        for key in sorted(set(ca[cid]) | set(cb[cid])):
            if ca[cid].get(key) != cb[cid].get(key):
                lines.append(f"~ {cid}.{key}: {ca[cid].get(key)!r} → {cb[cid].get(key)!r}")
    for key in ("global_rules", "sections", "scoring", "title"):
        if a.get(key) != b.get(key):
            lines.append(f"~ {key} изменён")
    return lines or ["(различий нет)"]
