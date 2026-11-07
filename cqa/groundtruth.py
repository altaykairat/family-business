"""S9. Ground truth: annotation format, drafts from agent runs, import, versions, tune/test split (G.1–G.6).

A ground-truth set is versioned like a checklist: data/ground_truth/<set>/v<N>.json, immutable, with author,
reason, parent and hash. Each label is a human statement about one conversation × criterion:

    {"answer": "no" | "yes" | "na" | <scale step>, "evidence_idx": [14], "comment": "…",
     "source": "consensus" | "human", "rev": "c07@r1"}

G.1: the source of truth is *people*. A draft is built from several agents: where all agents agree the label
is pre-filled as "consensus"; where they disagree the label is empty and marked "needs_review" with every
candidate shown — a human decides. Import refuses unresolved labels (or skips them with allow_partial).

The split (G.4) is fixed in the set: a stratified hold-out `test` part that is not used for tuning prompts.
"""

from __future__ import annotations

import copy
import csv
import random
from pathlib import Path
from typing import Optional

from . import checklist as ck
from . import common, events, scoring, sources, state
from . import responses as rs
from .common import CQAError, load_json, now_iso, save_json, stable_hash


def set_dir(set_id: str) -> Path:
    return common.GROUND_TRUTH / set_id


def versions(set_id: str) -> list:
    return sorted(int(p.stem[1:]) for p in set_dir(set_id).glob("v*.json") if p.stem[1:].isdigit())


def sets() -> list:
    root = common.GROUND_TRUTH
    out = []
    if root.exists():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            vs = versions(d.name)
            if vs:
                doc = load_json(d / f"v{vs[-1]}.json")
                out.append({"set_id": d.name, "versions": vs, "latest": vs[-1], "checklist": doc.get("checklist"),
                            "items": len(doc.get("items", {})), "split": {k: len(v) for k, v in
                                                                         (doc.get("split") or {}).items()
                                                                         if isinstance(v, list)}})
    return out


def load(spec: str) -> dict:
    """'dev-gold' (latest) or 'dev-gold@2' / 'gt:dev-gold@v2'."""
    spec = spec[3:] if spec.startswith("gt:") else spec
    sid, _, ver = spec.partition("@")
    vs = versions(sid)
    if not vs:
        raise CQAError(f"ground truth set {sid!r} not found", "not_found")
    v = int(ver.lstrip("v")) if ver else vs[-1]
    if v not in vs:
        raise CQAError(f"ground truth {sid}@v{v} not found", "not_found")
    return load_json(set_dir(sid) / f"v{v}.json")


# --- split ---------------------------------------------------------------------------

def group_of(conversation_id: str) -> str:
    """Stratum = id prefix: p1-ru, p1-kz, p1-en (chats by language), p4-ru (calls)."""
    return conversation_id.rsplit("-", 1)[0]


def make_split(ids: list, test_share: Optional[float] = None, seed: Optional[int] = None) -> dict:
    test_share = common.setting("ground_truth.test_share", 0.4) if test_share is None else test_share
    seed = common.setting("ground_truth.split_seed", 42) if seed is None else seed
    rng = random.Random(seed)
    groups: dict = {}
    for cid in sorted(ids):
        groups.setdefault(group_of(cid), []).append(cid)
    test = []
    for g in sorted(groups):
        members = groups[g][:]
        rng.shuffle(members)
        k = round(len(members) * test_share)
        if len(members) >= 2:
            k = min(max(k, 1), len(members) - 1)
        test += members[:k]
    return {"method": "stratified by id prefix", "seed": seed, "test_share": test_share,
            "tune": sorted(set(ids) - set(test)), "test": sorted(test)}


# --- drafts ---------------------------------------------------------------------------

def draft(source_specs: list, set_id: str, ids: Optional[list] = None, checklist_ref: Optional[str] = None,
          actor: str = "system") -> dict:
    """Pre-fill labels from several agents (consensus) and mark disagreements for a human decision."""
    loaded = [sources.load_results(s, ids) for s in source_specs]
    ref = checklist_ref or next((sources.checklist_ref(src, res) for src, res in loaded
                                 if sources.checklist_ref(src, res)), None) or "default"
    doc = ck.load(ref)
    conv_ids = sorted(set().union(*[set(res) for _, res in loaded])) if loaded else []
    if ids:
        conv_ids = [c for c in conv_ids if c in ids]
    revs = ck.criterion_revs(doc)
    items, open_labels = {}, 0
    for conv_id in conv_ids:
        labels = {}
        for c in ck.active_criteria(doc):
            cand = {}
            ev_idx = set()
            for src, res in loaded:
                r = res.get(conv_id)
                if not r:
                    continue
                a = (r.get("answers") or {}).get(c["id"])
                cand[src["label"]] = a
                for e in ((r.get("criteria") or {}).get(c["id"], {}).get("evidence") or []):
                    if isinstance(e.get("idx"), int):
                        ev_idx.add(e["idx"])
            present = [v for v in cand.values() if v is not None]
            agreed = len(present) == len(loaded) and len({str(v) for v in present}) == 1
            label = {"answer": present[0] if agreed else None, "evidence_idx": sorted(ev_idx),
                     "comment": "", "source": "consensus" if agreed else None,
                     "status": "consensus" if agreed else "needs_review", "candidates": cand, "rev": revs[c["id"]]}
            open_labels += 0 if agreed else 1
            labels[c["id"]] = label
        items[conv_id] = {"labels": labels, "annotator": None, "notes": ""}
    doc_out = {"meta": {"set_id": set_id, "kind": "draft", "created_at": now_iso(), "author": actor,
                        "sources": source_specs},
               "checklist": ck.ref_of(doc), "criteria_revs": revs, "split": make_split(conv_ids), "items": items}
    path = set_dir(set_id) / "draft.json"
    save_json(path, doc_out)
    events.append("gt_draft_created", actor=actor, set_id=set_id, path=common.rel(path), sources=source_specs,
                  conversations=len(items), needs_review=open_labels)
    return {"path": common.rel(path), "conversations": len(items), "needs_review": open_labels,
            "how": "fill `answer` (and evidence_idx/comment) for labels with status needs_review, set status to "
                   "'human', then gt.import"}


# --- import / versions ------------------------------------------------------------------

def _clean(doc: dict, data: dict, allow_partial: bool) -> tuple:
    crit = {c["id"]: c for c in ck.active_criteria(doc)}
    revs = ck.criterion_revs(doc)
    items, skipped, problems = {}, 0, []
    for conv_id, item in (data.get("items") or {}).items():
        labels = {}
        for cid, lab in (item.get("labels") or {}).items():
            if cid not in crit:
                problems.append(f"{conv_id}/{cid}: criterion not in {ck.ref_of(doc)}")
                continue
            if lab.get("answer") is None or lab.get("status") == "needs_review":
                if allow_partial:
                    skipped += 1
                    continue
                problems.append(f"{conv_id}/{cid}: label not resolved (needs_review)")
                continue
            ans, issue = rs.normalize_answer(lab["answer"], crit[cid])
            if ans is None or (issue and "округлено" in issue):
                problems.append(f"{conv_id}/{cid}: invalid answer {lab['answer']!r} ({issue})")
                continue
            labels[cid] = {"answer": ans, "evidence_idx": [i for i in lab.get("evidence_idx") or []
                                                           if isinstance(i, int)],
                           "comment": lab.get("comment", ""), "source": lab.get("source") or "human",
                           "rev": lab.get("rev") or revs[cid]}
        if labels:
            complete = set(labels) >= set(crit)
            sc = scoring.score(doc, {k: v["answer"] for k, v in labels.items()}) if complete else None
            items[conv_id] = {"labels": labels, "annotator": item.get("annotator"), "notes": item.get("notes", ""),
                              "complete": complete, "total": sc and sc["total"],
                              "gross_error": sc and sc["gross_error"]}
    return items, skipped, problems


def save_version(set_id: str, data: dict, actor: str, reason: str, allow_partial: bool = False) -> dict:
    ref = data.get("checklist") or "default"
    doc = ck.load(ref)
    items, skipped, problems = _clean(doc, data, allow_partial)
    if problems:
        raise CQAError("ground truth not imported:\n" + "\n".join(problems[:30]) +
                       (f"\n… and {len(problems) - 30} more" if len(problems) > 30 else ""))
    if not items:
        raise CQAError("no labels to import")
    vs = versions(set_id)
    split = data.get("split") or make_split(list(items))
    split = {**split, "tune": [c for c in split.get("tune", []) if c in items],
             "test": [c for c in split.get("test", []) if c in items]}
    missing = set(items) - set(split["tune"]) - set(split["test"])
    if missing:
        extra = make_split(sorted(missing), split.get("test_share"), split.get("seed"))
        split["tune"] = sorted(split["tune"] + extra["tune"])
        split["test"] = sorted(split["test"] + extra["test"])
    body = {"checklist": ck.ref_of(doc), "criteria_revs": ck.criterion_revs(doc), "split": split, "items": items}
    h = stable_hash(body)
    for v in vs:
        if stable_hash({k: load_json(set_dir(set_id) / f"v{v}.json")[k] for k in body}) == h:
            raise CQAError(f"identical to {set_id}@v{v} — nothing saved", "conflict")
    version = (vs[-1] + 1) if vs else 1
    out = {"meta": {"set_id": set_id, "version": version, "created_at": now_iso(), "author": actor,
                    "reason": reason, "parent_version": vs[-1] if vs else None, "content_hash": h,
                    "skipped_unresolved": skipped}, **body}
    path = set_dir(set_id) / f"v{version}.json"
    save_json(path, out)
    labels = sum(len(i["labels"]) for i in items.values())
    events.append("gt_version_created", actor=actor, set_id=set_id, version=version, path=common.rel(path),
                  content_hash=h, conversations=len(items), labels=labels, checklist=body["checklist"],
                  split={"tune": len(split["tune"]), "test": len(split["test"])}, reason=reason)
    return {"ref": f"{set_id}@v{version}", "path": common.rel(path), "conversations": len(items), "labels": labels,
            "skipped_unresolved": skipped, "split": {"tune": len(split["tune"]), "test": len(split["test"])}}


def import_file(path: str, set_id: str, actor: str, reason: str, allow_partial: bool = False,
                checklist_ref: Optional[str] = None) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        data = from_csv(p, checklist_ref)
    else:
        data = load_json(p)
    if checklist_ref:
        data["checklist"] = checklist_ref
    return save_version(set_id, data, actor, reason, allow_partial)


def from_csv(path: Path, checklist_ref: Optional[str] = None) -> dict:
    """CSV columns: conversation_id, criterion_id, answer, evidence_idx (\"14 15\"), comment, annotator."""
    items: dict = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            it = items.setdefault(row["conversation_id"], {"labels": {}, "annotator": row.get("annotator")})
            ev = [int(x) for x in (row.get("evidence_idx") or "").replace(",", " ").split() if x.strip().isdigit()]
            ans = row["answer"].strip()
            it["labels"][row["criterion_id"]] = {"answer": int(ans) if ans.lstrip("-").isdigit() else ans,
                                                 "evidence_idx": ev, "comment": row.get("comment", ""),
                                                 "source": "human"}
    return {"checklist": checklist_ref or "default", "items": items}


def update_labels(set_id: str, labels: list, actor: str, reason: str) -> dict:
    """New version with some labels changed: [{conversation_id, criterion_id, answer, evidence_idx, comment}]."""
    base = load(set_id)
    data = copy.deepcopy(base)
    for lab in labels:
        it = data["items"].setdefault(lab["conversation_id"], {"labels": {}, "annotator": actor, "notes": ""})
        it["labels"][lab["criterion_id"]] = {"answer": lab["answer"], "evidence_idx": lab.get("evidence_idx", []),
                                             "comment": lab.get("comment", ""), "source": "human"}
    return save_version(set_id, data, actor, reason)


# --- gold for metrics ------------------------------------------------------------------------

def gold(spec: str, split: str = "all") -> dict:
    """→ {'label', 'checklist', 'items': {conv: {crit: label}}, 'split'} from a GT set or from decisions."""
    if spec in ("decisions", "human"):
        st = state.load()
        items: dict = {}
        refs = set()
        for (conv, crit), d in st.human_truth().items():
            ap = st.appeals.get(d.get("appeal_id") or "", {})
            items.setdefault(conv, {})[crit] = {"answer": d["new_answer"],
                                                "evidence_idx": [ap["turn_idx"]] if ap.get("turn_idx") else [],
                                                "decision_id": d["decision_id"], "precedent": d["decision_id"] in st.precedent}
            refs.add(d.get("judged_on"))
        return {"label": "decisions", "checklist": sorted(r for r in refs if r)[-1] if refs else "default",
                "items": items, "split": None}
    data = load(spec)
    ids = list(data["items"])
    if split in ("tune", "test"):
        ids = [c for c in ids if c in set(data["split"][split])]
    elif split != "all":
        raise CQAError("split must be all, tune or test")
    label = f"{data['meta']['set_id']}@v{data['meta']['version']}" + ("" if split == "all" else f"[{split}]")
    return {"label": label, "checklist": data["checklist"],
            "items": {c: data["items"][c]["labels"] for c in ids}, "split": split}
