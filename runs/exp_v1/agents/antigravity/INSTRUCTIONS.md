# Инструкция для агента (antigravity) — Прогон `exp_v1`
Вариант декомпозиции: **v1**

1. Открой файлы в `prompts/`.
2. Для каждого файла скопируй его в чат облачной модели (или вызови CLI).
3. Ответ модели (строго JSON) сохрани в `responses/<то же имя без .md>.json` (или `.md`).
4. Запусти ingest: `python3 -m variants.antigravity ingest --run exp_v1`
