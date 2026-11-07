"""Shared helpers: paths, JSON I/O, hashing, config, errors and the append-only event log.

Paths. Read-only material (organizer data, templates, config) lives in the repository. Everything the
platform writes (checklist versions, ground truth, runs, history) lives in the *workspace*. By default the
workspace is the repository itself; `CQA_WORKSPACE=/some/dir` or `use_workspace()` (tests) moves it.
Modules must read paths as `common.RUNS`, `common.HISTORY`, … at call time, not copy them at import.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "prompts" / "templates"
CONFIG = ROOT / "config"
TASK_TEMPLATES = CONFIG / "task_templates"

WORKSPACE: Path
DATA: Path
CHECKLISTS: Path
GROUND_TRUTH: Path
RUNS: Path
HISTORY_DIR: Path
HISTORY: Path
BLOBS: Path
TASKS: Path
CACHE: Path
ATTACHMENTS: Path
REPORTS: Path


def use_workspace(path: Optional[Path] = None) -> Path:
    """Point every writable location at `path` (default: $CQA_WORKSPACE or the repository)."""
    global WORKSPACE, DATA, CHECKLISTS, GROUND_TRUTH, RUNS, HISTORY_DIR, HISTORY, BLOBS, TASKS, CACHE
    global ATTACHMENTS, REPORTS
    WORKSPACE = Path(path or os.environ.get("CQA_WORKSPACE") or ROOT).resolve()
    DATA = WORKSPACE / "data"
    CHECKLISTS = DATA / "checklists"
    GROUND_TRUTH = DATA / "ground_truth"
    RUNS = WORKSPACE / "runs"
    HISTORY_DIR = WORKSPACE / "history"
    HISTORY = HISTORY_DIR / "events.jsonl"
    BLOBS = HISTORY_DIR / "blobs"
    TASKS = HISTORY_DIR / "tasks"
    ATTACHMENTS = HISTORY_DIR / "attachments"
    REPORTS = HISTORY_DIR / "reports"
    CACHE = HISTORY_DIR / "cache"
    return WORKSPACE


use_workspace()


class CQAError(Exception):
    """User-facing error. `code` maps to an HTTP status in the API (bad_request, not_found, forbidden, conflict)."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code
        self.message = message


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(data: Any, length: int = 12) -> str:
    """Content hash of a JSON-serialisable object (key order independent)."""
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()[:length]


def text_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


def load_config() -> dict:
    """Pipeline parameters (config/pipeline.json) merged with pricing (config/pricing.json)."""
    cfg = load_json(CONFIG / "pipeline.json")
    pricing_file = CONFIG / "pricing.json"
    cfg["pricing"] = load_json(pricing_file) if pricing_file.exists() else {}
    return cfg


def platform_config() -> dict:
    """Platform parameters (config/platform.json): loop, metrics, analytics, API. Missing keys → defaults."""
    p = CONFIG / "platform.json"
    return load_json(p) if p.exists() else {}


def setting(path: str, default: Any = None) -> Any:
    """platform_config() lookup by dotted path, e.g. setting('metrics.evidence_window', 1)."""
    node: Any = platform_config()
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def log_event(event_type: str, **payload: Any) -> dict:
    """Append one event to the history log. Events are never edited or removed (decision 8)."""
    from . import events

    return events.append(event_type, **payload)


def rel(path: Path) -> str:
    p = Path(path).resolve()
    for base in (WORKSPACE, ROOT):
        try:
            return str(p.relative_to(base))
        except ValueError:
            continue
    return str(path)
