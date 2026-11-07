# Задача: Оценка качества диалога с верифицированным досье фактов (Stage 2 Judgment)

Ты — главный эксперт контроля качества (QA) Freedom Broker.
Перед тобой диалог #{{CONVERSATION_ID}} и проверенное **объективное досье фактов (Fact Dossier)**, извлечённое на первом этапе.

Твоя задача — вынести нормативное решение по каждому из критериев чек-листа «{{CHECKLIST_TITLE}}», опираясь на зафиксированные в досье факты и реплики диалога.

## Чек-лист: правила и критерии
{{GLOBAL_RULES}}

{{CRITERIA}}

## Досье фактов диалога (верифицировано на этапе 1)
{{DOSSIER_FACTS}}

## Транскрипт диалога #{{CONVERSATION_ID}}
{{DIALOG_META}}

```
{{DIALOG}}
```

## Алгоритмические факты (S2)
{{FACTS}}

## Формат ответа
Верни **только один JSON-объект** в блоке ```json. Никакого текста до или после блока.

```json
{
  "meta": {
    "conversation_id": "{{CONVERSATION_ID}}",
    "prompt_id": "{{PROMPT_ID}}"
  },
  "criteria": [
    {
      "criterion_id": "c01",
      "applicability": {
        "na_allowed": false,
        "na_condition_met": false,
        "reasoning": "вывод о применимости"
      },
      "sub_questions": [
        {"id": "sq_1", "question": "подвопрос", "answer": "да | нет | na", "evidence_idx": [0]}
      ],
      "rule_applied": "применённое правило / ступень",
      "alternatives_rejected": "почему не соседняя ступень / не другой ответ",
      "answer": "yes",
      "evidence": [
        {"idx": 0, "quote": "дословная цитата"}
      ],
      "checked_range": null,
      "explanation": "<Название> — <Ответ>. <Причина>. Основание — реплика #N.",
      "confidence": 1.0
    }
  ],
  "overall_comment": "краткое резюме работы оператора"
}
```
