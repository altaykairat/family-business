"""Prompt assembly engine for Antigravity fine-grained decomposition variants:

Variants (Axis B):
  v3_focus       — Laser-focused, criterion-by-criterion prompt (N calls / dialog)
  v4_facts       — Stage 1: Objective Fact Extraction Dossier (1 call / dialog)
  v4_mono        — Stage 2: Monolithic judgment grounded by Dossier (1 call / dialog; 2 total)
  v4_focus       — Stage 2: Criterion-by-criterion judgment grounded by Dossier (N calls / dialog; N+1 total)
  v_subq         — Atomic sub-question verification per criterion (N calls / dialog)
  v_subq_bundled — All criteria with atomic sub-questions bundled in 1 prompt (1 call / dialog)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from cqa.checklist import active_criteria, ref_of, section_title
from cqa.common import text_hash
from cqa.conversation import render_dialog, render_facts, render_meta

from .dossier import build_fact_extraction_prompt, render_dossier_markdown
from .subquestions import derive_subquestions

TEMPLATES_DIR = Path(__file__).parent / "templates"

TYPE_NAMES = {
    "binary": "Да / Нет",
    "scale": "Шкала баллов",
    "flag": "Грубая ошибка (флаг, без баллов)",
}


def _load_template(name: str) -> str:
    path = TEMPLATES_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Template {name} not found at {path}")
    return path.read_text(encoding="utf-8")


def _allowed_example(c: dict) -> str:
    if c["type"] == "scale":
        steps = [str(s["value"]) for s in c.get("scale", [])]
        return " | ".join(steps)
    if c.get("na_allowed"):
        return "yes | no | na"
    return "yes | no"


def _render_scale_steps(c: dict) -> str:
    if c["type"] != "scale":
        return ""
    lines = ["- **Ступени шкалы:**"]
    for s in c.get("scale", []):
        lines.append(f"  - **{s['value']}** — {s['text']}")
    return "\n".join(lines)


def _render_criterion_notes(c: dict) -> str:
    notes = c.get("notes", [])
    if not notes:
        return ""
    return "\n".join(f"- **Уточнение:** {n}" for n in notes)


def _render_subquestions_list(c: dict) -> str:
    sqs = derive_subquestions(c)
    lines = []
    for i, s in enumerate(sqs, 1):
        req = " [Обязательно]" if s.get("required") else ""
        lines.append(f"{i}. `{s['id']}`: {s['question']}{req}")
    return "\n".join(lines)


def _render_subquestions_table(c: dict) -> str:
    sqs = derive_subquestions(c)
    lines = ["| ID | Тип | Проверяемое условие |", "|---|---|---|"]
    for s in sqs:
        kind = s.get("kind", "clause")
        lines.append(f"| `{s['id']}` | {kind} | {s['question']} |")
    return "\n".join(lines)


def _render_bundled_criteria_with_subquestions(doc: dict) -> str:
    blocks = []
    for c in active_criteria(doc):
        allowed = _allowed_example(c)
        lines = [
            f"### {c['id']} · {c['title']} ({TYPE_NAMES.get(c['type'], c['type'])})",
            f"- **Секция:** {section_title(doc, c.get('section'))}; **Вес:** {c.get('weight', 0)} баллов",
            f"- **Формулировка:** {c['description']}",
            f"- **Допустимый ответ:** {allowed}",
        ]
        if c.get("na_allowed"):
            lines.append(f"- **N/A разрешён при условии:** {c.get('na_condition')}")
        if c["type"] == "scale":
            lines.append("- **Ступени шкалы:**")
            for s in c.get("scale", []):
                lines.append(f"  - **{s['value']}** — {s['text']}")
        lines.append("- **Атомарные подвопросы:**")
        sqs = derive_subquestions(c)
        for s in sqs:
            lines.append(f"  - `{s['id']}`: {s['question']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_prompts(
    variant: str,
    doc: dict,
    conv: dict,
    conv_facts: dict,
    dossier: Optional[dict] = None,
    include_facts: bool = True,
) -> List[Dict[str, Any]]:
    """Build prompts for conversation `conv` according to `variant`.

    Returns list of dicts:
      {
        'part': 'all' | 'facts' | '<criterion_id>',
        'criteria_ids': [...],
        'prompt_id': str,
        'template_hash': str,
        'text': str,
      }
    """
    crit = active_criteria(doc)
    all_crit_ids = [c["id"] for c in crit]
    cid = conv["conversation_id"]
    global_rules_text = "\n".join(f"{i}. {r}" for i, r in enumerate(doc.get("global_rules", []), 1))
    dialog_rendered = render_dialog(conv)
    dialog_meta = render_meta(conv)
    facts_rendered = render_facts(conv_facts) if include_facts else "(алгоритмические факты опущены)"

    out = []

    # 0. Variant: v1 (monolith baseline)
    if variant == "v1":
        import cqa.prompts as base_pr
        built = base_pr.build("v1", doc, conv, conv_facts, "v1", include_facts)
        return built

    # 1. Variant: v3_focus
    if variant == "v3_focus":
        tpl = _load_template("v3_focus")
        for c in crit:
            pid = f"v3_focus|{ref_of(doc)}|{cid}|{c['id']}"
            na_rule = (
                f"Разрешён. Условие неприменимости: «{c.get('na_condition')}»"
                if c.get("na_allowed")
                else "Не разрешён (ставить N/A запрещено)"
            )
            text = (
                tpl.replace("{{CRITERION_ID}}", c["id"])
                .replace("{{CRITERION_TITLE}}", c["title"])
                .replace("{{SECTION_TITLE}}", section_title(doc, c.get("section")))
                .replace("{{CRITERION_TYPE}}", TYPE_NAMES.get(c["type"], c["type"]))
                .replace("{{CRITERION_WEIGHT}}", str(c.get("weight", 0)))
                .replace("{{CRITERION_DESCRIPTION}}", c["description"])
                .replace("{{NA_RULE}}", na_rule)
                .replace("{{NA_ALLOWED_BOOL}}", "true" if c.get("na_allowed") else "false")
                .replace("{{SCALE_STEPS}}", _render_scale_steps(c))
                .replace("{{CRITERION_NOTES}}", _render_criterion_notes(c))
                .replace("{{GLOBAL_RULES}}", global_rules_text)
                .replace("{{SUBQUESTIONS_LIST}}", _render_subquestions_list(c))
                .replace("{{CONVERSATION_ID}}", cid)
                .replace("{{DIALOG_META}}", dialog_meta)
                .replace("{{DIALOG}}", dialog_rendered)
                .replace("{{FACTS}}", facts_rendered)
                .replace("{{ALLOWED_ANSWERS_EXAMPLE}}", _allowed_example(c))
                .replace("{{PROMPT_ID}}", pid)
            )
            out.append({
                "part": c["id"],
                "criteria_ids": [c["id"]],
                "prompt_id": pid,
                "template_hash": text_hash(tpl),
                "text": text,
            })
        return out

    # 2. Variant: v4_facts (Stage 1)
    if variant == "v4_facts":
        pid = f"v4_facts|{ref_of(doc)}|{cid}|facts"
        text = build_fact_extraction_prompt(doc, conv, conv_facts)
        out.append({
            "part": "facts",
            "criteria_ids": all_crit_ids,
            "prompt_id": pid,
            "template_hash": text_hash(text),
            "text": text,
        })
        return out

    # 3. Variant: v4_mono (Stage 2 monolithic with Dossier)
    if variant == "v4_mono":
        tpl = _load_template("v4_judge_mono")
        dossier_text = render_dossier_markdown(dossier or {})
        pid = f"v4_mono|{ref_of(doc)}|{cid}|all"
        criteria_text = _render_bundled_criteria_with_subquestions(doc)
        text = (
            tpl.replace("{{CONVERSATION_ID}}", cid)
            .replace("{{CHECKLIST_TITLE}}", doc.get("title", ""))
            .replace("{{GLOBAL_RULES}}", global_rules_text)
            .replace("{{CRITERIA}}", criteria_text)
            .replace("{{DOSSIER_FACTS}}", dossier_text)
            .replace("{{DIALOG_META}}", dialog_meta)
            .replace("{{DIALOG}}", dialog_rendered)
            .replace("{{FACTS}}", facts_rendered)
            .replace("{{PROMPT_ID}}", pid)
        )
        out.append({
            "part": "all",
            "criteria_ids": all_crit_ids,
            "prompt_id": pid,
            "template_hash": text_hash(tpl),
            "text": text,
        })
        return out

    # 4. Variant: v4_focus (Stage 2 focus with Dossier)
    if variant == "v4_focus":
        tpl = _load_template("v4_judge_focus")
        dossier_text = render_dossier_markdown(dossier or {})
        for c in crit:
            pid = f"v4_focus|{ref_of(doc)}|{cid}|{c['id']}"
            na_rule = (
                f"Разрешён. Условие неприменимости: «{c.get('na_condition')}»"
                if c.get("na_allowed")
                else "Не разрешён"
            )
            text = (
                tpl.replace("{{CRITERION_ID}}", c["id"])
                .replace("{{CRITERION_TITLE}}", c["title"])
                .replace("{{SECTION_TITLE}}", section_title(doc, c.get("section")))
                .replace("{{CRITERION_TYPE}}", TYPE_NAMES.get(c["type"], c["type"]))
                .replace("{{CRITERION_WEIGHT}}", str(c.get("weight", 0)))
                .replace("{{CRITERION_DESCRIPTION}}", c["description"])
                .replace("{{NA_RULE}}", na_rule)
                .replace("{{NA_ALLOWED_BOOL}}", "true" if c.get("na_allowed") else "false")
                .replace("{{SCALE_STEPS}}", _render_scale_steps(c))
                .replace("{{CRITERION_NOTES}}", _render_criterion_notes(c))
                .replace("{{GLOBAL_RULES}}", global_rules_text)
                .replace("{{DOSSIER_FACTS}}", dossier_text)
                .replace("{{CONVERSATION_ID}}", cid)
                .replace("{{DIALOG_META}}", dialog_meta)
                .replace("{{DIALOG}}", dialog_rendered)
                .replace("{{FACTS}}", facts_rendered)
                .replace("{{ALLOWED_ANSWERS_EXAMPLE}}", _allowed_example(c))
                .replace("{{PROMPT_ID}}", pid)
            )
            out.append({
                "part": c["id"],
                "criteria_ids": [c["id"]],
                "prompt_id": pid,
                "template_hash": text_hash(tpl),
                "text": text,
            })
        return out

    # 5. Variant: v_subq
    if variant == "v_subq":
        tpl = _load_template("v_subq")
        for c in crit:
            pid = f"v_subq|{ref_of(doc)}|{cid}|{c['id']}"
            na_rule = (
                f"Разрешён при условии: «{c.get('na_condition')}»"
                if c.get("na_allowed")
                else "Не разрешён"
            )
            text = (
                tpl.replace("{{CRITERION_ID}}", c["id"])
                .replace("{{CRITERION_TITLE}}", c["title"])
                .replace("{{SECTION_TITLE}}", section_title(doc, c.get("section")))
                .replace("{{CRITERION_TYPE}}", TYPE_NAMES.get(c["type"], c["type"]))
                .replace("{{CRITERION_WEIGHT}}", str(c.get("weight", 0)))
                .replace("{{CRITERION_DESCRIPTION}}", c["description"])
                .replace("{{NA_RULE}}", na_rule)
                .replace("{{SCALE_STEPS}}", _render_scale_steps(c))
                .replace("{{SUBQUESTIONS_TABLE}}", _render_subquestions_table(c))
                .replace("{{CONVERSATION_ID}}", cid)
                .replace("{{DIALOG_META}}", dialog_meta)
                .replace("{{DIALOG}}", dialog_rendered)
                .replace("{{FACTS}}", facts_rendered)
                .replace("{{ALLOWED_ANSWERS_EXAMPLE}}", _allowed_example(c))
                .replace("{{PROMPT_ID}}", pid)
            )
            out.append({
                "part": c["id"],
                "criteria_ids": [c["id"]],
                "prompt_id": pid,
                "template_hash": text_hash(tpl),
                "text": text,
            })
        return out

    # 6. Variant: v_subq_bundled
    if variant == "v_subq_bundled":
        tpl = _load_template("v_subq_bundled")
        pid = f"v_subq_bundled|{ref_of(doc)}|{cid}|all"
        bundled_text = _render_bundled_criteria_with_subquestions(doc)
        rules_block = ""
        if doc.get("rules"):
            rules_block = "## Уточнения правил\n\n" + "\n".join(f"- {r}" for r in doc["rules"]) + "\n"
        precedents_block = ""
        if doc.get("precedents"):
            precedents_block = "## Прецеденты\n\n" + "\n".join(f"- {p}" for p in doc["precedents"]) + "\n"

        text = (
            tpl.replace("{{CONVERSATION_ID}}", cid)
            .replace("{{CHECKLIST_TITLE}}", doc.get("title", ""))
            .replace("{{GLOBAL_RULES}}", global_rules_text)
            .replace("{{RULES}}", rules_block)
            .replace("{{PRECEDENTS}}", precedents_block)
            .replace("{{BUNDLED_CRITERIA_WITH_SUBQUESTIONS}}", bundled_text)
            .replace("{{DIALOG_META}}", dialog_meta)
            .replace("{{DIALOG}}", dialog_rendered)
            .replace("{{FACTS}}", facts_rendered)
            .replace("{{PROMPT_ID}}", pid)
        )
        out.append({
            "part": "all",
            "criteria_ids": all_crit_ids,
            "prompt_id": pid,
            "template_hash": text_hash(tpl),
            "text": text,
        })
        return out

    raise ValueError(f"Unknown Antigravity variant {variant!r}")
