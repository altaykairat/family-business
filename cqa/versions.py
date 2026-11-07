"""Version registry for everything except checklists (which have their own files): prompt templates,
pipeline parameters, S2 feature extractors, model mapping, platform settings (decision 9, VC.6–VC.7).

A version is an immutable snapshot stored as a blob and announced by a `config_version_registered`
event: kind, name, version number, content hash, parent, author, reason. Identical content is never
registered twice (VC.11). `bundle()` builds the full set of versions an evaluation refers to.
"""

from __future__ import annotations

import difflib
import json
from typing import Optional

from . import checklist as ck
from . import common, events
from .common import CQAError, load_json, save_json, stable_hash, text_hash

KINDS = ("template", "params", "features", "models", "platform", "task_template")
FEATURE_PARAMS = ("gap_threshold_s",)


def _registered(kind: str, name: str, until: Optional[str] = None) -> list:
    return [e for e in events.read(until=until, types={"config_version_registered"})
            if e.get("kind") == kind and e.get("name") == name]


def register(kind: str, name: str, content, author: str = "system", reason: str = "") -> dict:
    """Register a snapshot; returns {'kind','name','version','hash','blob'} (existing one if unchanged)."""
    if kind not in KINDS:
        raise CQAError(f"unknown version kind {kind!r}, expected {KINDS}")
    h = text_hash(content) if isinstance(content, str) else stable_hash(content)
    known = _registered(kind, name)
    for e in known:
        if e["content_hash"] == h:
            return {"kind": kind, "name": name, "version": e["version"], "hash": h, "blob": e["blob"]}
    blob = events.put_blob({"kind": kind, "name": name, "content": content})
    version = len(known) + 1
    events.append("config_version_registered", actor=author, kind=kind, name=name, version=version,
                  content_hash=h, blob=blob, parent=known[-1]["version"] if known else None, reason=reason)
    return {"kind": kind, "name": name, "version": version, "hash": h, "blob": blob}


def history(kind: Optional[str] = None, name: Optional[str] = None, until: Optional[str] = None) -> list:
    out = []
    for e in events.read(until=until, types={"config_version_registered"}):
        if (kind is None or e["kind"] == kind) and (name is None or e["name"] == name):
            out.append({k: e.get(k) for k in ("kind", "name", "version", "content_hash", "blob", "parent",
                                              "reason", "actor", "ts")})
    return out


def get(kind: str, name: str, version: int) -> dict:
    for e in _registered(kind, name):
        if e["version"] == int(version):
            return {"kind": kind, "name": name, "version": e["version"], "hash": e["content_hash"],
                    "content": events.get_blob(e["blob"])["content"], "ts": e["ts"], "reason": e.get("reason")}
    raise CQAError(f"{kind} {name} v{version} not registered", "not_found")


def diff(kind: str, name: str, a: int, b: int) -> list:
    """Unified diff (text snapshots) or key-by-key diff (JSON snapshots) between two versions."""
    ca, cb = get(kind, name, a)["content"], get(kind, name, b)["content"]
    if isinstance(ca, str) and isinstance(cb, str):
        return list(difflib.unified_diff(ca.splitlines(), cb.splitlines(), f"{name}@v{a}", f"{name}@v{b}",
                                         lineterm=""))
    fa = _flatten(ca)
    fb = _flatten(cb)
    return [f"~ {k}: {json.dumps(fa.get(k), ensure_ascii=False)} → {json.dumps(fb.get(k), ensure_ascii=False)}"
            for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)] or ["(различий нет)"]


def _flatten(obj, prefix: str = "") -> dict:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): obj}


# --- what an evaluation refers to ---------------------------------------------------

def code_hash(*modules: str) -> str:
    parts = []
    for m in modules:
        p = common.ROOT / "cqa" / f"{m}.py"
        parts.append(p.read_text(encoding="utf-8") if p.exists() else "")
    return text_hash("\n".join(parts))


def bundle(doc: dict, cfg: dict, template_name: str, author: str = "system") -> dict:
    """Full set of versions for one run: checklist + criterion revisions, template, parameters, S2 features,
    model mapping, scoring/validation code. Registers snapshots that are new."""
    template_path = common.TEMPLATES / f"{template_name}.md"
    template_text = template_path.read_text(encoding="utf-8") if template_path.exists() else ""
    params = {k: v for k, v in cfg.items() if k not in ("pricing",)}
    features = {"params": {k: cfg.get(k) for k in FEATURE_PARAMS}, "code": code_hash("conversation")}
    tpl = register("template", template_name, template_text, author, "used by a run")
    par = register("params", "pipeline", params, author, "used by a run")
    fea = register("features", "s2", features, author, "used by a run")
    mod = register("models", "agent_models", cfg.get("agent_models", {}), author, "used by a run")
    return {
        "checklist": {"ref": ck.ref_of(doc), "hash": doc["meta"]["content_hash"],
                      "criteria_revs": ck.criterion_revs(doc), "clarification_revs": ck.clarification_revs(doc)},
        "template": {"name": template_name, "version": tpl["version"], "hash": tpl["hash"]},
        "params": {"version": par["version"], "hash": par["hash"]},
        "features": {"version": fea["version"], "hash": fea["hash"]},
        "models": {"version": mod["version"], "mapping": cfg.get("agent_models", {})},
        "code": {"scoring": code_hash("scoring"), "validation": code_hash("responses"),
                 "prompts": code_hash("prompts")},
    }


# --- editing pipeline parameters (admin, decision 3) ---------------------------------

def set_param(key: str, value, author: str, reason: str) -> dict:
    """Change one key of config/pipeline.json and register the new parameter version."""
    path = common.CONFIG / "pipeline.json"
    cfg = load_json(path)
    old = cfg.get(key)
    if old == value:
        raise CQAError(f"{key} already equals {value!r}", "conflict")
    cfg[key] = value
    save_json(path, cfg)
    reg = register("params", "pipeline", cfg, author, reason)
    events.append("config_changed", actor=author, kind="params", key=key, old=old, new=value, reason=reason,
                  version=reg["version"])
    return {"key": key, "old": old, "new": value, "params_version": reg["version"]}
