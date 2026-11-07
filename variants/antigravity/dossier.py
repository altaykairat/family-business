"""Fact Dossier Engine for V4 (Two-stage Fact Extraction & Grounded Judgment).

Stage 1 extracts an objective, non-judgmental Conversation Dossier.
Stage 2 uses the Dossier as verified ground truth for final checklist scoring.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from cqa.checklist import active_criteria


def build_fact_extraction_prompt(doc: dict, conv: dict, s2_facts: dict) -> str:
    """Generate the Stage 1 prompt to extract an objective Conversation Dossier."""
    crit = active_criteria(doc)
    crit_titles = [f"- {c['id']}: {c['title']} ({c['type']})" for c in crit]

    lines = [
        f"#{t['idx']} [{t['timestamp']}] {t['speaker']}: {t['text']}"
        for t in conv["transcript"]
    ]
    dialog_text = "\n".join(lines)

    prompt = f"""# Задача: Извлечение объективных фактов диалога (Fact Extraction Dossier)

Ты — беспристрастный аудитор контакт-центра Freedom Broker. Твоя задача — извлечь **только проверенные факты** о ходе диалога ниже, БЕЗ выставления оценок, баллов или субъективных суждений.

Каждый факт должен подтверждаться конкретным номером реплики (`#idx`) и дословной цитатой (`quote`). Не додумывай факты.

## Контекст чек-листа
В дальнейшем эти факты будут использоваться для проверки следующих критериев:
{chr(10).join(crit_titles)}

## Транскрипт диалога #{conv['conversation_id']} ({conv.get('channel', 'чат')}, язык исходника {conv.get('language', 'RU')})

```
{dialog_text}
```

## Алгоритмические метки времени (S2)
- Первая реплика оператора: #{s2_facts.get('first_operator_idx')}
- Тип концовки: {s2_facts.get('ending_type')}
- Длинные паузы (секунды): {s2_facts.get('turn_gaps')}

## Формат ответа
Верни **только один валидный JSON-объект** в блоке ```json. Никакого текста до или после блока.

```json
{{
  "conversation_id": "{conv['conversation_id']}",
  "language_audit": {{
    "client_language": "RU | KZ | EN | mixed",
    "operator_language_first_turn": "RU | KZ | EN | mixed",
    "languages_match": true,
    "evidence_idx": 0,
    "quote": "..."
  }},
  "contact_audit": {{
    "first_operator_turn_idx": 0,
    "greeting_present": true,
    "operator_introduced_name": "Имя оператора или null",
    "mask_used": false,
    "greeting_and_intro_in_first_turn": true,
    "evidence_idx": 0,
    "quote": "..."
  }},
  "client_request_audit": {{
    "client_questions": ["суть каждого вопроса клиента"],
    "initial_data_sufficient": true,
    "operator_clarified_missing": false,
    "bot_provided_solution_before_operator": false
  }},
  "security_audit": {{
    "protected_action_or_account_data_requested": false,
    "request_details": "что конкретно запросил клиент (тариф, баланс, вывод и т.д.)",
    "verification_attempted": false,
    "verification_confirmed_turn_idx": null,
    "protected_action_turn_idx": null,
    "verified_before_protected_action": null
  }},
  "consultation_audit": {{
    "resolved_questions": ["вопросы, на которые дан полный ответ"],
    "unresolved_questions": ["вопросы, оставшиеся без ответа или с ошибкой"],
    "next_step_given": "указан ли правильный дальнейший шаг",
    "overall_coverage": "full | main_only | partial | general_hint | none"
  }},
  "empathy_audit": {{
    "client_dissatisfaction_expressed": false,
    "dissatisfaction_quotes": [],
    "operator_addressed_dissatisfaction": null,
    "operator_tone": "polite | neutral | rude"
  }},
  "conduct_audit": {{
    "explicit_waits_without_warning": [],
    "client_asked_to_clarify_confusing_turn": false,
    "dialog_flow_confusing": false
  }},
  "ending_audit": {{
    "how_dialog_ended": "operator_closed | client_left | connection_lost | normal_goodbye | other",
    "operator_had_chance_to_say_goodbye": true,
    "operator_offered_help_or_summary": false,
    "operator_said_goodbye": false,
    "ending_evidence_idx": 0,
    "quote": "..."
  }},
  "violations_audit": {{
    "insult_detected": false,
    "off_platform_deal_offered": false,
    "confidential_data_leaked": false,
    "notes": "пояснение при наличии нарушений"
  }}
}}
```
"""
    return prompt


def validate_dossier(conv: dict, data: dict) -> Tuple[dict, List[str]]:
    """Validate extracted Dossier structure and referenced turns against the transcript."""
    issues = []
    turns = {t["idx"]: t for t in conv["transcript"]}

    if not isinstance(data, dict):
        return {}, ["Dossier data is not a JSON object"]

    # Verify referenced turn indices
    def check_idx(idx: Any, field_name: str):
        if idx is not None and isinstance(idx, int):
            if idx not in turns:
                issues.append(f"Dossier field {field_name} references non-existent turn #{idx}")

    check_idx(data.get("language_audit", {}).get("evidence_idx"), "language_audit.evidence_idx")
    check_idx(data.get("contact_audit", {}).get("first_operator_turn_idx"), "contact_audit.first_operator_turn_idx")
    check_idx(data.get("contact_audit", {}).get("evidence_idx"), "contact_audit.evidence_idx")
    check_idx(data.get("security_audit", {}).get("verification_confirmed_turn_idx"), "security_audit.verification_confirmed_turn_idx")
    check_idx(data.get("security_audit", {}).get("protected_action_turn_idx"), "security_audit.protected_action_turn_idx")
    check_idx(data.get("ending_audit", {}).get("ending_evidence_idx"), "ending_audit.ending_evidence_idx")

    return data, issues


def render_dossier_markdown(dossier: dict) -> str:
    """Format the structured dossier as a Markdown report for injection into Stage 2 prompts."""
    if not dossier:
        return "(Досье фактов не предоставлено)"

    lang = dossier.get("language_audit", {})
    contact = dossier.get("contact_audit", {})
    req = dossier.get("client_request_audit", {})
    sec = dossier.get("security_audit", {})
    cons = dossier.get("consultation_audit", {})
    emp = dossier.get("empathy_audit", {})
    cond = dossier.get("conduct_audit", {})
    ending = dossier.get("ending_audit", {})
    viol = dossier.get("violations_audit", {})

    lines = [
        "### Объективное досье фактов диалога (Fact Extraction Stage)",
        f"- **Язык общения:** клиент={lang.get('client_language')}, оператор в первом ответе={lang.get('operator_language_first_turn')}, совпадение={lang.get('languages_match')}",
        f"- **Первый контакт (реплика #{contact.get('first_operator_turn_idx')}):** приветствие={contact.get('greeting_present')}, имя оператора={contact.get('operator_introduced_name')}, в первом ответе={contact.get('greeting_and_intro_in_first_turn')}",
        f"- **Запрос клиента:** {', '.join(req.get('client_questions', [])) or 'не сформулирован'}. Хватало ли данных сразу: {req.get('initial_data_sufficient')}",
        f"- **Идентификация и безопасность:** защищённое действие запрошено={sec.get('protected_action_or_account_data_requested')}. Проверка подтверждена={sec.get('verification_attempted')} (до действия: {sec.get('verified_before_protected_action')})",
        f"- **Полнота решения:** закрыто={cons.get('resolved_questions')}, не закрыто={cons.get('unresolved_questions')}, статус={cons.get('overall_coverage')}",
        f"- **Эмпатия и недовольство:** клиент выражал недовольство={emp.get('client_dissatisfaction_expressed')}, оператор отреагировал={emp.get('operator_addressed_dissatisfaction')}, тон={emp.get('operator_tone')}",
        f"- **Ведение диалога:** пауз без предупреждения={len(cond.get('explicit_waits_without_warning', []))}, переспросы клиента={cond.get('client_asked_to_clarify_confusing_turn')}",
        f"- **Завершение:** сценарий={ending.get('how_dialog_ended')}, была возможность попрощаться={ending.get('operator_had_chance_to_say_goodbye')}, помощь предложена={ending.get('operator_offered_help_or_summary')}, попрощался={ending.get('operator_said_goodbye')}",
        f"- **Грубые нарушения:** оскорбление={viol.get('insult_detected')}, обход компании={viol.get('off_platform_deal_offered')}, утечка={viol.get('confidential_data_leaked')}",
    ]
    return "\n".join(lines)
