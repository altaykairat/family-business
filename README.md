# CQA — Conversation Quality Analytics

Прототип для хакатона Freedom Broker: оценка работы операторов контакт-центра (чаты RU / KZ / EN и звонки) по настраиваемому чек-листу с объяснениями, цитатами-доказательствами и контуром апелляций.

**Сейчас:** итерация 1 — база пайплайна в ручном режиме. Промпты генерируются из чек-листа, ответы облачных агентов (Claude Code, Codex, Antigravity) вставляются вручную, система проверяет их, считает баллы и сравнивает агентов лоб в лоб.

## Быстрый старт

Нужен только Python 3.9+ (без зависимостей).

```bash
python3 -m cqa prepare --run r001        # промпты для всех 65 диалогов DEV × 3 агента
# … вставить ответы в runs/r001/agents/<agent>/responses/ …
python3 -m cqa ingest --run r001          # проверка ответов и подсчёт баллов
python3 -m cqa report --run r001          # отчёт: runs/r001/reports/summary.md
python3 -m unittest discover -s tests     # тесты
```

Обновить данные из API организаторов (нужен `.env`, см. `.env.example`):

```bash
python3 scripts/fetch_dataset.py DEV
```

## Документы

| Документ | Что внутри |
|---|---|
| [docs/pipeline.md](docs/pipeline.md) | Как устроен пайплайн, папки прогона, порядок работы |
| [docs/discussion.md](docs/discussion.md) | Журнал обсуждений и принятые решения |
| [docs/design-space.md](docs/design-space.md) | Каталог параметров, метрик и протоколов |
| [plan.md](plan.md) | Исходный план и анализ задачи |
| [docs/task.md](docs/task.md), [docs/checklist.md](docs/checklist.md) | Задание и чек-лист организаторов |
| [data/README.md](data/README.md) | Данные и их формат |

## Структура

```
cqa/                 пайплайн: checklist, conversation, prompts, responses, scoring, run, report, cli
prompts/templates/   шаблоны промптов
config/              параметры пайплайна и цены моделей
data/                чек-листы (версии) и наборы диалогов
runs/                прогоны: промпты, ответы агентов, проверки, отчёты
history/             журнал событий (только добавление)
tests/               тесты
```
