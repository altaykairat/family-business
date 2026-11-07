"""Manual paste helper: next waiting prompt of a run → clipboard; clipboard → response file, checked at once.

Works for any run folder with the layout `agents/<agent>/prompts/*.md` + `agents/<agent>/responses/`:
platform runs (`runs/<run>`), variant runs (`variants/*/runs/<run>`) or an explicit path. Prompt order follows
the run manifest when it lists conversations, otherwise file names. The «current» prompt is always the first
one without a response, so `next` and `save` agree without any hidden state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from . import common
from .common import CQAError, load_json, rel

RESPONSE_EXT = (".json", ".md", ".txt")


def resolve_run(run: str) -> Path:
    """`run` = run id under runs/, a variant run id (variants/*/runs/<id>) or a path to a run folder."""
    cands = [Path(run), common.RUNS / run] + sorted(common.ROOT.glob(f"variants/*/runs/{run}"))
    for p in cands:
        if (p / "agents").is_dir():
            return p.resolve()
    raise CQAError(f"прогон {run} не найден (ищу runs/{run}, variants/*/runs/{run} и путь как есть)", "not_found")


def agents_of(run_dir: Path) -> list:
    m = run_dir / "manifest.json"
    listed = load_json(m).get("agents") if m.exists() else None
    found = sorted(p.name for p in (run_dir / "agents").iterdir() if (p / "prompts").is_dir())
    return [a for a in (listed or found) if a in found]


def prompt_names(run_dir: Path, agent: str) -> list:
    box = run_dir / "agents" / agent / "prompts"
    on_disk = {p.stem for p in box.glob("*.md")}
    order = []
    m = run_dir / "manifest.json"
    if m.exists():
        for info in (load_json(m).get("conversations") or {}).values():
            for part in info.get("parts") or []:
                name = Path(part.get("file", "")).stem
                if name in on_disk and name not in order:
                    order.append(name)
    return order + sorted(on_disk - set(order))


def response_path(run_dir: Path, agent: str, name: str) -> Optional[Path]:
    for ext in RESPONSE_EXT:
        p = run_dir / "agents" / agent / "responses" / f"{name}{ext}"
        if p.exists() and p.read_text(encoding="utf-8").strip():
            return p
    return None


def pick_agent(run_dir: Path, agent: Optional[str]) -> str:
    agents = agents_of(run_dir)
    if not agents:
        raise CQAError(f"в {rel(run_dir)} нет папок agents/<agent>/prompts", "not_found")
    if agent:
        if agent not in agents:
            raise CQAError(f"агента {agent} нет в прогоне; есть: {', '.join(agents)}")
        return agent
    for a in agents:  # first agent that still has work
        if any(response_path(run_dir, a, n) is None for n in prompt_names(run_dir, a)):
            return a
    return agents[0]


def status(run: str, agent: str = None) -> dict:
    run_dir = resolve_run(run)
    out = {"run": rel(run_dir), "agents": {}}
    for a in ([agent] if agent else agents_of(run_dir)):
        names = prompt_names(run_dir, a)
        waiting = [n for n in names if response_path(run_dir, a, n) is None]
        out["agents"][a] = {"total": len(names), "done": len(names) - len(waiting), "waiting": len(waiting),
                            "next": waiting[0] if waiting else None}
    return out


def current(run: str, agent: str = None) -> dict:
    """The first prompt without a response (what `next` copies and `save` fills)."""
    run_dir = resolve_run(run)
    a = pick_agent(run_dir, agent)
    names = prompt_names(run_dir, a)
    waiting = [n for n in names if response_path(run_dir, a, n) is None]
    if not waiting:
        return {"run": rel(run_dir), "agent": a, "name": None, "left": 0, "total": len(names)}
    name = waiting[0]
    text = (run_dir / "agents" / a / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return {"run": rel(run_dir), "agent": a, "name": name, "text": text, "left": len(waiting), "total": len(names),
            "number": len(names) - len(waiting) + 1}


def _platform_context(run_dir: Path, name: str):
    """(conversation, checklist doc, expected criteria, threshold) when the run is a platform run, else None."""
    from . import checklist as ck
    from . import conversation as cv
    from . import prompts as pr

    m = run_dir / "manifest.json"
    if not m.exists():
        return None
    man = load_json(m)
    if man.get("variant") not in pr.VARIANTS or not man.get("checklist"):
        return None
    cid = name.split("__")[0]
    info = (man.get("conversations") or {}).get(cid)
    if not info:
        return None
    part = next((p for p in info["parts"] if Path(p["file"]).stem == name), None)
    try:
        conv = cv.load(man["dataset"], cid)
        doc = ck.load(man["checklist"].replace("@v", "@"))
    except CQAError:
        return None
    threshold = (man.get("params") or {}).get("quote_match_threshold", 0.9)
    return conv, doc, (part or {}).get("criteria_ids"), threshold


def check(run_dir: Path, name: str, text: str, prompt_text: str = None) -> dict:
    """Parse at once. Errors → do not save, ask to re-paste. Platform runs are also validated against the dialog."""
    from . import responses as rs

    if not (text or "").strip():
        return {"ok": False, "errors": ["буфер пуст"], "warnings": []}
    if prompt_text and text.strip() == prompt_text.strip():
        return {"ok": False, "errors": ["в буфере всё ещё промпт, а не ответ модели"], "warnings": []}
    data, err = rs.parse(text)
    if err:
        return {"ok": False, "errors": [err], "warnings": []}
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["ответ — не JSON-объект"], "warnings": []}
    ctx = _platform_context(run_dir, name)
    if ctx is None:
        return {"ok": True, "errors": [], "warnings": [], "validated": False}
    conv, doc, expected, threshold = ctx
    v = rs.validate(conv, doc, data, threshold, expected)
    fmt = lambda i: f"{i['criterion_id'] or '—'}: {i['msg']}"  # noqa: E731
    errors = [fmt(i) for i in v["issues"] if i["level"] == "error"]
    warnings = [fmt(i) for i in v["issues"] if i["level"] != "error"]
    return {"ok": not errors, "errors": errors, "warnings": warnings, "validated": True}


def save(run: str, text: str, agent: str = None, name: str = None, force: bool = False,
         actor: str = "system") -> dict:
    """Save `text` as the response to the current prompt (or `name`). Rejected answers are not saved."""
    run_dir = resolve_run(run)
    a = pick_agent(run_dir, agent)
    if name is None:
        cur = current(str(run_dir), a)
        if cur["name"] is None:
            raise CQAError("все промпты этого агента уже с ответами", "conflict")
        name = cur["name"]
    prompt_file = run_dir / "agents" / a / "prompts" / f"{name}.md"
    if not prompt_file.exists():
        raise CQAError(f"промпта {name} нет у агента {a}", "not_found")
    res = check(run_dir, name, text, prompt_file.read_text(encoding="utf-8"))
    out = {"run": rel(run_dir), "agent": a, "name": name, **res, "saved": None}
    if not res["ok"] and not force:
        return out
    box = run_dir / "agents" / a / "responses"
    box.mkdir(parents=True, exist_ok=True)
    for ext in RESPONSE_EXT:  # one response per prompt
        old = box / f"{name}{ext}"
        if old.exists():
            old.unlink()
    path = box / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    out["saved"] = rel(path)
    common.log_event("response_submitted", actor=actor, run_id=run_dir.name, run_path=rel(run_dir), agent=a,
                     prompt=name, conversation_id=name.split("__")[0], forced=bool(force and not res["ok"]))
    return out


# --- clipboard (macOS pbcopy / pbpaste; xclip as a fallback) ----------------------------------------

def copy(text: str) -> None:
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise CQAError("нет pbcopy / xclip — скопируйте файл промпта вручную", "not_implemented")


def paste() -> str:
    for cmd in (["pbpaste"], ["xclip", "-selection", "clipboard", "-o"]):
        try:
            return subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8")
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise CQAError("нет pbpaste / xclip", "not_implemented")
