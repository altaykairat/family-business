"""S3. Prompt assembly: template + checklist version + conversation + facts → paste-ready text.

Decomposition variants (axis B):
  v1 — all criteria in one prompt (monolith)
  v2 — one prompt per section
  v3 — one prompt per criterion
"""

from __future__ import annotations

from .checklist import active_criteria, ref_of, section_title
from .common import TEMPLATES, text_hash
from .conversation import render_dialog, render_facts, render_meta

VARIANTS = ("v1", "v2", "v3")

TYPE_LABEL = {
    "binary": "Да / Нет",
    "scale": "шкала",
    "flag": "Да / Нет (флаг, без баллов)",
}


def render_criterion(doc: dict, c: dict) -> str:
    allowed = ["`yes`", "`no`"] if c["type"] != "scale" else [f"`{s['value']}`" for s in c["scale"]]
    if c.get("na_allowed"):
        allowed.append("`na`")
    lines = [
        f"### {c['id']} · {c['title']}",
        f"- Секция: {section_title(doc, c.get('section'))}",
        f"- Тип ответа: {TYPE_LABEL[c['type']]}; допустимые значения `answer`: {', '.join(allowed)}",
        f"- Вес: {c.get('weight', 0)}" + (" (без баллов, отмечается отдельно)" if c["type"] == "flag" else ""),
        f"- Формулировка: {c['description']}",
    ]
    if c.get("na_allowed"):
        lines.append(f"- N/A разрешён, условие: {c.get('na_condition')}")
    else:
        lines.append("- N/A не разрешён")
    if c["type"] == "scale":
        lines.append("- Ступени шкалы:")
        lines += [f"  - **{s['value']}** — {s['text']}" for s in c["scale"]]
    for note in c.get("notes", []):
        lines.append(f"- Уточнение: {note}")
    return "\n".join(lines)


def parts(doc: dict, variant: str) -> list:
    """[(part_id, [criteria])] for the chosen decomposition."""
    crit = active_criteria(doc)
    if variant == "v1":
        return [("all", crit)]
    if variant == "v2":
        order = [s["id"] for s in doc.get("sections", [])]
        groups = {}
        for c in crit:
            groups.setdefault(c.get("section") or "none", []).append(c)
        return [(sid, groups[sid]) for sid in order + sorted(set(groups) - set(order)) if sid in groups]
    if variant == "v3":
        return [(c["id"], [c]) for c in crit]
    raise SystemExit(f"Unknown variant {variant!r}, expected {VARIANTS}")


def build(template_name: str, doc: dict, conv: dict, conv_facts: dict, variant: str,
          include_facts: bool = True) -> list:
    """Returns [{'part', 'criteria_ids', 'prompt_id', 'text'}] for one conversation."""
    template = (TEMPLATES / f"{template_name}.md").read_text(encoding="utf-8")
    all_ids = [c["id"] for c in active_criteria(doc)]
    out = []
    for part_id, crit in parts(doc, variant):
        ids = [c["id"] for c in crit]
        scope = ("" if ids == all_ids else
                 f"**В этом запросе оцени только критерии: {', '.join(ids)}.** Остальные критерии чек-листа "
                 f"оцениваются отдельно, их не включай.")
        prompt_id = f"{template_name}|{ref_of(doc)}|{variant}|{part_id}"
        filled = {
            "CHECKLIST_TITLE": doc.get("title", ""),
            "SCOPE_NOTE": scope,
            "GLOBAL_RULES": "\n".join(f"{i}. {r}" for i, r in enumerate(doc.get("global_rules", []), 1)),
            "CRITERIA": "\n\n".join(render_criterion(doc, c) for c in crit),
            "DIALOG_META": render_meta(conv),
            "DIALOG": render_dialog(conv),
            "FACTS": render_facts(conv_facts) if include_facts else "(в этом прогоне не передаются)",
            "CRITERIA_IDS": ", ".join(ids),
            "CONVERSATION_ID": conv["conversation_id"],
            "PROMPT_ID": prompt_id,
        }
        text = template
        for key, value in filled.items():
            text = text.replace("{{" + key + "}}", value)
        text = text.replace("\n\n\n", "\n\n")
        out.append({"part": part_id, "criteria_ids": ids, "prompt_id": prompt_id,
                    "template_hash": text_hash(template), "text": text})
    return out
