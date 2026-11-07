"""Shared helpers: paths, JSON I/O, hashing, config and the append-only event log."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CHECKLISTS = DATA / "checklists"
RUNS = ROOT / "runs"
TEMPLATES = ROOT / "prompts" / "templates"
CONFIG = ROOT / "config"
HISTORY = ROOT / "history" / "events.jsonl"


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stable_hash(data: Any, length: int = 12) -> str:
    """Content hash of a JSON-serialisable object (key order independent)."""
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def text_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config() -> dict:
    """Pipeline parameters (config/pipeline.json) merged with pricing (config/pricing.json)."""
    cfg = load_json(CONFIG / "pipeline.json")
    pricing_file = CONFIG / "pricing.json"
    cfg["pricing"] = load_json(pricing_file) if pricing_file.exists() else {}
    return cfg


def log_event(event_type: str, **payload: Any) -> dict:
    """Append one event to history/events.jsonl. Events are never edited or removed."""
    event = {"ts": now_iso(), "type": event_type, **payload}
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)
