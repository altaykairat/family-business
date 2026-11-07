"""Where evaluation results live. A *source* is a folder of per-conversation result files in the base format
`…/validated/<agent>/<conversation_id>.json` — produced by the base pipeline (runs/), by the variants of
Antigravity and Codex (runs/ or variants/<name>/runs/), or by aggregation (validated/majority/).

Source specs:
  r001:claude                       runs/r001/validated/claude (also searched in variants/*/runs/r001)
  codex:pilot-review-v1:claude      variants/codex/runs/pilot-review-v1/validated/claude
  path/to/validated/claude          any folder with <id>.json files
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import common
from .common import CQAError, load_json


def _manifest_near(folder: Path) -> tuple:
    for parent in list(folder.parents)[:3]:
        m = parent / "manifest.json"
        if m.exists():
            return load_json(m), parent
    return None, None


def resolve(spec: str) -> dict:
    """→ {'label', 'dir', 'run_id', 'agent', 'manifest', 'run_dir'}"""
    candidates = []
    p = Path(spec)
    if p.is_dir() or (common.ROOT / spec).is_dir():
        folder = p if p.is_dir() else common.ROOT / spec
        candidates.append(folder)
    else:
        parts = spec.split(":")
        if len(parts) == 2:
            run, agent = parts
            candidates.append(common.RUNS / run / "validated" / agent)
            candidates += sorted((common.ROOT / "variants").glob(f"*/runs/{run}/validated/{agent}"))
        elif len(parts) == 3:
            variant, run, agent = parts
            candidates.append(common.ROOT / "variants" / variant / "runs" / run / "validated" / agent)
        else:
            raise CQAError(f"bad source {spec!r}: expected run:agent, variant:run:agent or a folder")
    folder = next((c for c in candidates if c.is_dir()), None)
    if folder is None:
        raise CQAError(f"source {spec!r} not found (looked in: {', '.join(common.rel(c) for c in candidates)})",
                       "not_found")
    manifest, run_dir = _manifest_near(folder)
    return {"label": spec, "dir": folder, "agent": folder.name,
            "run_id": (manifest or {}).get("run_id") or (run_dir.name if run_dir else folder.parent.parent.name),
            "manifest": manifest, "run_dir": run_dir}


def load_results(spec: str, ids: Optional[list] = None) -> tuple:
    """(source info, {conversation_id: result dict})"""
    src = resolve(spec)
    out = {}
    for f in sorted(src["dir"].glob("*.json")):
        if f.name.endswith(".usage.json"):
            continue
        if ids and f.stem not in ids:
            continue
        data = load_json(f)
        cid = data.get("conversation_id") or f.stem
        out[cid] = data
    return src, out


def checklist_ref(src: dict, results: dict) -> Optional[str]:
    m = src.get("manifest") or {}
    if m.get("checklist"):
        return m["checklist"]
    for r in results.values():
        if r.get("checklist"):
            return r["checklist"]
    return None


def dataset_of(src: dict) -> str:
    return (src.get("manifest") or {}).get("dataset") or "dev"
