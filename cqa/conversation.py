"""S1 normalization and S2 deterministic facts for one conversation (no LLM)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .common import DATA, load_json

ROLE_RU = {"client": "client", "operator": "operator", "bot": "bot", "system": "system"}


def dataset_dir(dataset: str) -> Path:
    return DATA / dataset.lower()


def list_ids(dataset: str) -> list:
    return sorted(p.stem for p in (dataset_dir(dataset) / "conversations").glob("*.json"))


def load(dataset: str, conversation_id: str) -> dict:
    rec = load_json(dataset_dir(dataset) / "conversations" / f"{conversation_id}.json")
    return normalize(rec)


# --- S1: normalization -------------------------------------------------------

def _parse_ts(ts: str):
    if re.fullmatch(r"\d{1,3}:\d{2}", ts):
        m, s = ts.split(":")
        return int(m) * 60 + int(s)
    norm = re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00")
    return datetime.fromisoformat(norm)


def _fmt(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def normalize(rec: dict) -> dict:
    """Adds `idx` (1-based), `t` (seconds from start) and `dup` (repeat of the previous turn)."""
    turns = rec["transcript"]
    start = _parse_ts(turns[0]["timestamp"]) if turns else 0
    out, prev = [], None
    for i, t in enumerate(turns, 1):
        cur = _parse_ts(t["timestamp"])
        secs = int((cur - start).total_seconds()) if isinstance(cur, datetime) else cur - start
        turn = {
            "idx": t.get("idx", i),
            "timestamp": t["timestamp"],
            "t": secs,
            "speaker": t["speaker"],
            "text": t["text"],
            "dup": bool(prev and prev["speaker"] == t["speaker"] and prev["text"] == t["text"]),
        }
        out.append(turn)
        prev = t
    return {**rec, "transcript": out, "is_call": rec.get("channel") == "voice call"}


def render_dialog(conv: dict) -> str:
    lines = []
    for t in conv["transcript"]:
        when = f"+{_fmt(t['t'])}" if conv["is_call"] else t["timestamp"]
        text = " ".join(t["text"].split())
        dup = "  [повтор предыдущей реплики]" if t["dup"] else ""
        lines.append(f"#{t['idx']} [{when}] {t['speaker']}: {text}{dup}")
    return "\n".join(lines)


def render_meta(conv: dict) -> str:
    return (f"- ID: {conv['conversation_id']}\n- Канал: {conv['channel']}\n- Язык набора: {conv['language']}\n"
            f"- Реплик: {len(conv['transcript'])}")


# --- S2: deterministic facts -------------------------------------------------

def facts(conv: dict, gap_threshold: int = 30) -> dict:
    tr = conv["transcript"]
    ops = [t for t in tr if t["speaker"] == "operator"]
    first_op = ops[0] if ops else None
    before_first = [t for t in tr if first_op and t["idx"] < first_op["idx"]]
    escalation = next((t for t in reversed(before_first) if t["speaker"] == "system"), None)
    last = tr[-1] if tr else None
    clients = [t for t in tr if t["speaker"] == "client"]
    last_client = clients[-1] if clients else None

    # waits: client message(s) → next operator reply, excluding the first operator reply
    gaps = []
    pending_client = None
    seen_operator = False
    for t in tr:
        if t["speaker"] == "client" and pending_client is None:
            pending_client = t
        elif t["speaker"] == "operator":
            if seen_operator and pending_client is not None:
                secs = t["t"] - pending_client["t"]
                if secs >= gap_threshold:
                    gaps.append({"client_idx": pending_client["idx"], "operator_idx": t["idx"], "seconds": secs})
            seen_operator = True
            pending_client = None

    return {
        "escalation_idx": escalation["idx"] if escalation else None,
        "first_operator_turn_idx": first_op["idx"] if first_op else None,
        "operator_turn_idxs": [t["idx"] for t in ops],
        "time_to_first_operator_reply_s": (first_op["t"] - escalation["t"]) if first_op and escalation else None,
        "operator_reply_gaps_s": gaps,
        "gap_threshold_s": gap_threshold,
        "last_turn": {"idx": last["idx"], "speaker": last["speaker"], "text": last["text"]} if last else None,
        "last_client_idx": last_client["idx"] if last_client else None,
        "seconds_from_last_client_to_end": (last["t"] - last_client["t"]) if last and last_client else None,
        "last_operator_idx": ops[-1]["idx"] if ops else None,
        "duplicate_turn_idxs": [t["idx"] for t in tr if t["dup"]],
    }


def render_facts(f: dict) -> str:
    gaps = ", ".join(f"после #{g['client_idx']} → ответ #{g['operator_idx']} через {g['seconds']} с"
                     for g in f["operator_reply_gaps_s"]) or "нет"
    last = f["last_turn"]
    lines = [
        f"- Эскалация на оператора: #{f['escalation_idx']}" if f["escalation_idx"] else "- Эскалация: не найдена",
        f"- Первая реплика оператора: #{f['first_operator_turn_idx']}",
        f"- Все реплики оператора: {', '.join('#' + str(i) for i in f['operator_turn_idxs']) or 'нет'}",
        f"- Время до первого ответа оператора: {f['time_to_first_operator_reply_s']} с (в оценку не входит)",
        f"- Паузы оператора ≥ {f['gap_threshold_s']} с после реплики клиента (кроме первого ответа): {gaps}",
        f"- Последняя реплика: #{last['idx']} ({last['speaker']}): «{last['text']}»" if last else "- Последняя реплика: нет",
        f"- От последней реплики клиента до конца диалога: {f['seconds_from_last_client_to_end']} с",
    ]
    if f["duplicate_turn_idxs"]:
        lines.append(f"- Повторы реплик (артефакт расшифровки): {', '.join('#' + str(i) for i in f['duplicate_turn_idxs'])}")
    return "\n".join(lines)


# --- operator id guess (decision 20) ----------------------------------------

_NAME_PATTERNS = [
    r"[Мм]еня зовут\s+([А-ЯЁA-Z][а-яёa-z]+)",
    r"(?:^|[\s.!,])[Яя]\s+([А-ЯЁ][а-яё]+)",
    r"\bI'?m\s+([A-Z][a-z]+)",
    r"[Мм]енің атым\s+([А-ЯӘІҢҒҮҰҚӨҺ][а-яәіңғүұқөһ]+)",
    r"(?:^|[\s.!,])[Мм]ен\s+([А-ЯӘІҢҒҮҰҚӨҺ][а-яәіңғүұқөһ]+)",
]
_KZ_SUFFIX = re.compile(r"(мын|мін|пын|пін|бын|бін)$")


def guess_operator_name(conv: dict) -> Optional[str]:
    op = next((t for t in conv["transcript"] if t["speaker"] == "operator"), None)
    if not op:
        return None
    for pat in _NAME_PATTERNS:
        m = re.search(pat, op["text"])
        if m:
            return _KZ_SUFFIX.sub("", m.group(1))
    return None
