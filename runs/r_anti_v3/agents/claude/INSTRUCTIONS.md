# Инструкция для агента (claude) — Прогон `r_anti_v3`
Вариант декомпозиции: **v3_focus**

1. Открой файлы в `prompts/`.
2. Для каждого файла скопируй его в чат облачной модели (или вызови CLI).
3. Ответ модели (строго JSON) сохрани в `responses/<то же имя без .md>.json` (или `.md`).
4. Запусти ingest: `python3 -m variants.antigravity ingest --run r_anti_v3`
