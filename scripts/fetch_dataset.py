#!/usr/bin/env python3
"""Fetch a CQA dataset by group_id and save it as git-friendly text files.

Usage:
    python3 scripts/fetch_dataset.py DEV                 # download from the organizers' API
    python3 scripts/fetch_dataset.py DEV --from-file raw.json   # re-export an already saved response

The API endpoint is read from the CQA_API_URL environment variable or from .env
(the URL without the trailing &group_id=...). Output goes to data/<group_id>/:

    raw.json                  original API response, pretty-printed
    index.md / index.csv      one row per conversation
    conversations/<id>.json   record with message index `idx` added to each turn
    transcripts/<id>.md       human-readable transcript
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_api_url() -> str:
    url = os.environ.get("CQA_API_URL")
    env_file = ROOT / ".env"
    if not url and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("CQA_API_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("CQA_API_URL is not set (export it or add it to .env, see .env.example)")
    return url


def fetch(group_id: str) -> dict:
    base = load_api_url()
    parts = urllib.parse.urlsplit(base)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "group_id"]
    query.append(("group_id", group_id))
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_ts(ts: str):
    """Seconds for MM:SS chat timestamps, datetime for ISO 8601 call timestamps."""
    if re.fullmatch(r"\d{1,3}:\d{2}", ts):
        m, s = ts.split(":")
        return int(m) * 60 + int(s)
    # Python < 3.11 accepts at most 6 fractional digits
    norm = re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00")
    return datetime.fromisoformat(norm)


def offset_label(ts: str, start) -> str:
    """Relative offset from the first turn as M:SS (used for audio seeking in calls)."""
    cur = parse_ts(ts)
    secs = int((cur - start).total_seconds()) if isinstance(cur, datetime) else cur - start
    return f"{secs // 60}:{secs % 60:02d}"


def transcript_md(rec: dict) -> str:
    turns = rec["transcript"]
    speakers = Counter(t["speaker"] for t in turns)
    is_call = rec["channel"] == "voice call"
    lines = [
        f"# {rec['conversation_id']}",
        "",
        f"- **Канал:** {rec['channel']}",
        f"- **Язык:** {rec['language']}",
        f"- **Реплик:** {len(turns)} ({', '.join(f'{k}: {v}' for k, v in speakers.most_common())})",
    ]
    if rec.get("audio_url"):
        lines.append(f"- **Запись:** [плеер]({rec['audio_url']})")
    lines += ["", "| # | Время | Роль | Текст |", "|---|---|---|---|"]
    start = parse_ts(turns[0]["timestamp"]) if turns else None
    for i, t in enumerate(turns, 1):
        when = f"+{offset_label(t['timestamp'], start)}" if is_call else t["timestamp"]
        text = t["text"].replace("|", "\\|").replace("\n", "<br>")
        role = f"**{t['speaker']}**" if t["speaker"] == "operator" else t["speaker"]
        lines.append(f"| {i} | {when} | {role} | {text} |")
    if is_call:
        lines += ["", "_Время звонка показано как смещение от первой реплики; исходные ISO-метки — в JSON._"]
    return "\n".join(lines) + "\n"


def export(resp: dict, group_id: str) -> Path:
    if not resp.get("isSuccess"):
        sys.exit(f"API error: {resp.get('error')!r}")
    data = resp["data"]
    out = ROOT / "data" / group_id.lower()
    (out / "conversations").mkdir(parents=True, exist_ok=True)
    (out / "transcripts").mkdir(parents=True, exist_ok=True)

    (out / "raw.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = []
    for rec in data["records"]:
        cid = rec["conversation_id"]
        enriched = dict(rec)
        enriched["transcript"] = [{"idx": i, **t} for i, t in enumerate(rec["transcript"], 1)]
        (out / "conversations" / f"{cid}.json").write_text(
            json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "transcripts" / f"{cid}.md").write_text(transcript_md(rec), encoding="utf-8")
        sp = Counter(t["speaker"] for t in rec["transcript"])
        rows.append({
            "conversation_id": cid,
            "channel": rec["channel"],
            "language": rec["language"],
            "turns": len(rec["transcript"]),
            "operator_turns": sp.get("operator", 0),
            "client_turns": sp.get("client", 0),
            "has_audio": bool(rec.get("audio_url")),
            "last_event": rec["transcript"][-1]["text"] if rec["transcript"] and rec["transcript"][-1]["speaker"] == "system" else "",
        })

    with open(out / "index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    by = Counter((r["channel"], r["language"]) for r in rows)
    md = [
        f"# Набор {data.get('dataset_group_id', group_id)}",
        "",
        f"Всего диалогов: **{data.get('total_records', len(rows))}**. "
        + ", ".join(f"{ch} {lang}: {n}" for (ch, lang), n in sorted(by.items())),
        "",
        "| ID | Канал | Язык | Реплик | Оператор | Последнее системное событие |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| [{r['conversation_id']}](transcripts/{r['conversation_id']}.md) | {r['channel']} | {r['language']} "
            f"| {r['turns']} | {r['operator_turns']} | {r['last_event']} |"
        )
    (out / "index.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("group_id", help="dataset group, e.g. DEV")
    ap.add_argument("--from-file", type=Path, help="use a saved API response instead of downloading")
    args = ap.parse_args()
    resp = json.loads(args.from_file.read_text(encoding="utf-8")) if args.from_file else fetch(args.group_id)
    out = export(resp, args.group_id)
    print(f"Saved {len(resp['data']['records'])} conversations to {out.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
