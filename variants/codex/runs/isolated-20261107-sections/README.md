# Ручной прогон

Каждый prompts/*.md вставьте в новый чат соответствующей модели. Ответ сохраните в responses/<имя>.json или .md. Не выполняйте промпты силами разработчика.
Опционально responses/<имя>.usage.json: {"input_tokens": 123, "output_tokens": 456, "model": "..."}.
Далее: python3 -m variants.codex ingest --run <эта папка>. Для review затем route и снова ingest.
