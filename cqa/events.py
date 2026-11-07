"""Append-only event log (decision 8) with a hash chain (VC.12) and a content-addressed blob store (VC.14).

history/events.jsonl — one JSON event per line, never edited or removed. Every new event carries
`seq`, `id`, `ts`, `type`, `actor`, its payload, `prev` (hash of the previous line) and `hash` (hash of itself),
so any later edit of the history is detectable with `verify()`. Lines written before the chain existed are
accepted as-is and simply become the start of the chain.

history/blobs/<h[:2]>/<h>.json — immutable JSON documents addressed by their content hash (full evaluation
results, template and parameter snapshots). Events reference blobs by hash; the same content is stored once.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from . import common
from .common import CQAError, canonical, now_iso, parse_iso

try:  # POSIX file locking; on other systems appends are still atomic enough for one user
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def _event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:16]


def append(event_type: str, actor: Optional[str] = None, **payload: Any) -> dict:
    """Append one event and return it (with seq, id, hash)."""
    path: Path = common.HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
            seq = len(lines) + 1
            event = {"seq": seq, "id": f"e{seq:06d}", "ts": now_iso(), "type": event_type,
                     "actor": actor or payload.pop("actor", None) or "system"}
            payload.pop("actor", None)
            for key, value in payload.items():
                if key in event:
                    raise CQAError(f"event payload key {key!r} is reserved")
                event[key] = value
            event["prev"] = _line_hash(lines[-1]) if lines else None
            event["hash"] = _event_hash(event)
            f.seek(0, 2)
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
    return event


def read(until: Optional[str] = None, types: Optional[Iterable[str]] = None) -> list:
    """All events in order. `until` (ISO time) — only events at or before that moment (time machine)."""
    path: Path = common.HISTORY
    if not path.exists():
        return []
    wanted = set(types) if types else None
    limit = parse_iso(until) if until else None
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        ev = json.loads(line)
        ev.setdefault("seq", n)
        ev.setdefault("id", f"e{n:06d}")
        ev.setdefault("actor", "system")
        if limit is not None and parse_iso(ev["ts"]) > limit:
            break
        if wanted is None or ev["type"] in wanted:
            out.append(ev)
    return out


def fingerprint() -> str:
    """Cheap identity of the current log state (size + last line) — used to know when caches are stale."""
    path: Path = common.HISTORY
    if not path.exists():
        return "empty"
    data = path.read_bytes()
    tail = data.rstrip().rsplit(b"\n", 1)[-1]
    return f"{len(data)}:{hashlib.sha256(tail).hexdigest()[:16]}"


def verify() -> dict:
    """Check the hash chain. Returns {'ok', 'events', 'chained', 'problems': [...]}."""
    path: Path = common.HISTORY
    problems, chained, prev_line, total = [], 0, None, 0
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            total += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                problems.append({"line": n, "problem": "not valid JSON"})
                prev_line = line
                continue
            if "hash" in ev:
                chained += 1
                if _event_hash(ev) != ev["hash"]:
                    problems.append({"line": n, "id": ev.get("id"), "problem": "event content does not match its hash"})
                expected_prev = _line_hash(prev_line) if prev_line else None
                if ev.get("prev") != expected_prev:
                    problems.append({"line": n, "id": ev.get("id"), "problem": "chain broken: previous line changed"})
            elif chained:
                problems.append({"line": n, "problem": "unchained event after the chain started"})
            prev_line = line
    return {"ok": not problems, "events": total, "chained": chained, "problems": problems}


# --- blobs -------------------------------------------------------------------

def put_blob(obj: Any) -> str:
    h = hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:24]
    path = common.BLOBS / h[:2] / f"{h}.json"
    if not path.exists():
        common.save_json(path, obj)
    return h


def get_blob(h: str) -> Any:
    path = common.BLOBS / h[:2] / f"{h}.json"
    if not path.exists():
        raise CQAError(f"blob {h} not found", "not_found")
    return common.load_json(path)
