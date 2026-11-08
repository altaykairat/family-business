# CQA — Conversation Quality Analytics

Прототип для хакатона Freedom Broker: оценка работы операторов контакт-центра (чаты RU / KZ / EN и звонки) по настраиваемому чек-листу с объяснениями, цитатами-доказательствами и контуром апелляций.

**Сейчас:** итерация 1 — база пайплайна в ручном режиме. Промпты генерируются из чек-листа, ответы LLM вставляются вручную (новые чаты облачных моделей вместо API), система проверяет их, считает баллы и сравнивает модели лоб в лоб.

## Быстрый старт

Нужен только Python 3.9+ (без зависимостей).

**Интерфейс и API:**

```bash
python3 -m cqa serve                      # http://127.0.0.1:8765 — интерфейс, API: /api/v1/commands
```

В интерфейсе: роли (администратор, руководитель, проверяющий, оператор), список диалогов с фильтром грубых ошибок и апелляций, карточка оценки с доказательствами, апелляция от оператора и решение проверяющего (тип A–D), чек-лист с версиями, активацией и пересчётом без LLM, ручная оценка диалога через вставку промпта в чат модели, «Откуда ответ» — промпт, сырой ответ и версии. Вёрстка и стиль — прототип Адильхана (ветка `adilkhan`), подключённый к API платформы.

**Пайплайн из командной строки:**

```bash
python3 -m cqa prepare --run r001        # промпты для всех 65 диалогов DEV × 3 модели
# … вставить ответы в runs/r001/agents/<agent>/responses/ …
python3 -m cqa ingest --run r001          # проверка ответов и подсчёт баллов
python3 -m cqa report --run r001          # отчёт: runs/r001/reports/summary.md
python3 -m unittest discover -s tests     # тесты
```

Обновить данные из API организаторов (нужен `.env`, см. `.env.example`):

```bash
python3 scripts/fetch_dataset.py DEV
```

## Откуда что: материалы организаторов и наши

### От организаторов — правила и данные, которые мы не меняем

| Путь | Что это |
|---|---|
| [docs/organizers/task.md](docs/organizers/task.md) | Задание хакатона (текстовая версия PDF) |
| [docs/organizers/checklist.md](docs/organizers/checklist.md) | Чек-лист: 8 критериев, шкалы, правила расчёта, памятка |
| [docs/organizers/criteria.md](docs/organizers/criteria.md) | Критерии отбора топ-50 |
| [docs/organizers/results-format.md](docs/organizers/results-format.md) | Как сдать результаты: `results/dev` и `results/prod`, формат JSON 1.0, коды критериев, контроль за 1 час до стоп-кода |
| [docs/organizers/announcements.md](docs/organizers/announcements.md) | Сообщения организаторов по ходу хакатона: отбор по совокупности факторов, корпоративный стиль интерфейса, живая демонстрация, подход к оценке точности |
| [docs/organizers/company.md](docs/organizers/company.md) | Презентация компании (фон) |
| `docs/organizers/*.pdf` | Исходные PDF (локально, не в git) |
| `data/dev/` | Набор DEV из API организаторов: `raw.json` — без изменений; `conversations/`, `transcripts/`, `index.*` — наша раскладка по файлам |
| [data/checklists/default/v1.json](data/checklists/default/v1.json) | Чек-лист организаторов в машиночитаемом виде — версия v1, не меняется |

### Наше — обсуждения, решения, планы, код

| Путь | Что это |
|---|---|
| [docs/team/discussion.md](docs/team/discussion.md) | Журнал обсуждений и принятые решения (главный документ дизайна) |
| [docs/team/design-space.md](docs/team/design-space.md) | Каталог параметров, метрик и протоколов |
| [docs/team/pipeline.md](docs/team/pipeline.md) | Как устроен текущий пайплайн, папки прогона, порядок работы |
| [docs/team/plan.md](docs/team/plan.md) | Исходный план и анализ задачи |
| [docs/team/labeling/](docs/team/labeling/README.md) | Разметка нашего эталона DEV: промпт для чата-помощника, порции диалогов, свод спорных трактовок, метки |
| [docs/team/agent-briefs/](docs/team/agent-briefs/README.md) | Кто что делает параллельно, задания для сессий и агентов |
| `docs/team/variants/`, `variants/` | Альтернативные прототипы пайплайна (Antigravity, Codex) |
| `data/checklists/<id>/v2+` | Наши версии чек-листа (появятся при изменениях) |
| `cqa/`, `scripts/`, `tests/` | Код |
| `prompts/templates/` | Шаблоны промптов |
| `config/` | Параметры пайплайна и цены моделей |
| `runs/` | Прогоны: промпты, ответы моделей, проверки, отчёты |
| `history/` | Журнал событий (только добавление) |

## Структура кода

```
cqa/                 пайплайн и платформа: checklist, conversation, prompts, responses, scoring, run, report, api, server, …
web/                 интерфейс (index.html, app.js, style.css), раздаётся `python3 -m cqa serve`
prompts/templates/   шаблоны промптов
config/              параметры пайплайна и цены моделей
data/                чек-листы (версии) и наборы диалогов
runs/                прогоны
history/             журнал событий
tests/               тесты
```
