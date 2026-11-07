# Задача: Проверка атомарных условий по критерию {{CRITERION_ID}} («{{CRITERION_TITLE}}»)

Ты — контролёр соблюдения стандартов качества контакт-центра Freedom Broker.
Твоя задача — проверить выполнение каждого **атомарного подвопроса** для критерия:
**{{CRITERION_ID}} · {{CRITERION_TITLE}}**.

## Определение критерия из чек-листа
- **Секция:** {{SECTION_TITLE}}
- **Тип:** {{CRITERION_TYPE}}
- **Вес:** {{CRITERION_WEIGHT}}
- **Формулировка:** {{CRITERION_DESCRIPTION}}
- **Правило N/A:** {{NA_RULE}}
{{SCALE_STEPS}}

## Список атомарных подвопросов для проверки
{{SUBQUESTIONS_TABLE}}

## Транскрипт диалога #{{CONVERSATION_ID}}
{{DIALOG_META}}

```
{{DIALOG}}
```

## Алгоритмические факты (S2)
{{FACTS}}

## Формат ответа
Верни **только один JSON-объект** в блоке ```json. Для каждого подвопроса укажи: `value` (true/false), номер реплики (`evidence_idx`) и дословную цитату (`quote`).

```json
{
  "meta": {
    "conversation_id": "{{CONVERSATION_ID}}",
    "prompt_id": "{{PROMPT_ID}}",
    "criterion_id": "{{CRITERION_ID}}"
  },
  "sub_questions": [
    {
      "id": "sq_id",
      "value": true,
      "evidence_idx": 0,
      "quote": "дословная цитата или null",
      "reasoning": "краткое подтверждение"
    }
  ],
  "synthesized_answer": "{{ALLOWED_ANSWERS_EXAMPLE}}",
  "explanation": "{{CRITERION_TITLE}} — [Ответ]. [Суть]. Основание — [реплики]."
}
```
