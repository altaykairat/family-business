"""S3. Prompt assembly: template + checklist version + conversation + facts → paste-ready text.

Decomposition variants (axis B):
  v1 — all criteria in one prompt (monolith)
  v2 — one prompt per section
  v3 — one prompt per criterion
"""

from __future__ import annotations

from .checklist import active_clarifications, active_criteria, clarification_revs, ref_of, section_title
from .common import TEMPLATES, stable_hash, text_hash
from .conversation import render_dialog, render_facts, render_meta

VARIANTS = ("v1", "v2", "v3")

TYPE_LABEL = {
    "binary": "Да / Нет",
    "scale": "шкала",
    "flag": "Да / Нет (флаг, без баллов)",
}


RULES_HEADER = ("## Уточнения правил\n\n"
                "Уточнения приняты проверяющими по итогам разбора спорных случаев. Они дополняют формулировки "
                "чек-листа выше; где уточнение конкретнее формулировки — следуй уточнению.")
PRECEDENTS_HEADER = ("## Прецеденты\n\n"
                     "Решения проверяющих по похожим ситуациям в других диалогах. Если ситуация в этом диалоге "
                     "совпадает — применяй ту же логику; если отличается — назови отличие в `alternatives_rejected`.")


def render_rules(items: list) -> str:
    """{{RULES}}: a whole section with a heading, or an empty string when there is nothing to insert."""
    if not items:
        return ""
    lines = [RULES_HEADER, ""]
    for n, u in enumerate(items, 1):
        scope = f"**{u['criterion_id']}** — " if u.get("criterion_id") else "**Все критерии** — "
        reason = f" *Причина:* {u['reason']}" if u.get("reason") else ""
        lines.append(f"{n}. {scope}{u['text']}{reason}")
    return "\n".join(lines)


def render_precedents(items: list) -> str:
    """{{PRECEDENTS}}: situation (turns), reviewer's decision and reason; empty string when none were selected."""
    if not items:
        return ""
    lines = [PRECEDENTS_HEADER]
    for n, p in enumerate(items, 1):
        frag = "\n".join(f"#{t['idx']} {t['speaker']}: {' '.join(t['text'].split())}" for t in p.get("fragment") or [])
        lines += ["", f"### Прецедент {n} · {p['criterion_id']}",
                  f"- Ответ модели: `{p.get('model_answer')}` → решение проверяющего: `{p.get('reviewer_answer')}`",
                  f"- Причина: {p.get('reason') or '—'}",
                  "- Ситуация (реплики другого диалога):", "", "```", frag, "```"]
    return "\n".join(lines)


def render_criterion(doc: dict, c: dict, inline_clarifications: tuple = ()) -> str:
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
    for u in inline_clarifications:  # templates without {{RULES}} (v1) still receive the clarifications
        if u.get("criterion_id") == c["id"]:
            lines.append(f"- Уточнение ({u['id']}): {u['text']}")
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
          include_facts: bool = True, precedents: list = None, only: list = None) -> list:
    """Returns [{'part', 'criteria_ids', 'prompt_id', 'text', 'context'}] for one conversation.

    `context` = which rule clarifications (with revisions) and which precedents (decision ids) went into the
    prompt; it is part of prompt_id, so two prompts with different rules/precedents never share an id."""
    template = (TEMPLATES / f"{template_name}.md").read_text(encoding="utf-8")
    all_ids = [c["id"] for c in active_criteria(doc)]
    has_rules = "{{RULES}}" in template
    has_precedents = "{{PRECEDENTS}}" in template
    revs = clarification_revs(doc) if doc.get("clarifications") else {}
    out = []
    for part_id, crit in parts(doc, variant):
        if only:  # partial re-evaluation: only new / changed criteria (scenario «добавить критерий»)
            crit = [c for c in crit if c["id"] in only]
            if not crit:
                continue
        ids = [c["id"] for c in crit]
        rules = active_clarifications(doc, ids)
        global_rules = list(doc.get("global_rules", []))
        if not has_rules:
            global_rules += [u["text"] for u in rules if not u.get("criterion_id")]
        prec = [p for p in (precedents or []) if p["criterion_id"] in ids] if has_precedents else []
        context = {"rules": {u["id"]: revs.get(u["id"], u["id"]) for u in rules},
                   "precedents": [p["decision_id"] for p in prec]}
        scope = ("" if ids == all_ids else
                 f"**В этом запросе оцени только критерии: {', '.join(ids)}.** Остальные критерии чек-листа "
                 f"оцениваются отдельно, их не включай.")
        prompt_id = f"{template_name}|{ref_of(doc)}|{variant}|{part_id}"
        if context["rules"] or context["precedents"]:
            context["hash"] = stable_hash(context, 8)
            prompt_id += f"|ctx:{context['hash']}"
        filled = {
            "CHECKLIST_TITLE": doc.get("title", ""),
            "SCOPE_NOTE": scope,
            "GLOBAL_RULES": "\n".join(f"{i}. {r}" for i, r in enumerate(global_rules, 1)),
            "CRITERIA": "\n\n".join(render_criterion(doc, c, () if has_rules else tuple(rules)) for c in crit),
            "RULES": render_rules(rules),
            "PRECEDENTS": render_precedents(prec),
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
        while "\n\n\n" in text:  # empty sections leave no gaps
            text = text.replace("\n\n\n", "\n\n")
        out.append({"part": part_id, "criteria_ids": ids, "prompt_id": prompt_id,
                    "template_hash": text_hash(template), "text": text, "context": context})
    return out
