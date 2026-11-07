# Пайплайн — итерация 1 (ручной режим) + платформа

> Python 3.9+, только стандартная библиотека. Поверх пайплайна итерации 1 — платформа: журнал событий, версии, апелляции, эталон и метрики, цикл обучения, аналитика, HTTP API. Подробно — [implementation.md](implementation.md). LLM — облачные агенты (Claude Code, Codex, Antigravity), промпты вставляются вручную. Решения, на которых это основано, — в [discussion.md](discussion.md) (решения 1–21), параметры — в [design-space.md](design-space.md).

## Этапы и где они в коде

| Этап | Что делает | Модуль | Артефакт |
|---|---|---|---|
| **S0** Конфигурация | Версии чек-листа: неизменяемые снимки, diff, архив | [`cqa/checklist.py`](../../cqa/checklist.py) | `data/checklists/<id>/v<N>.json` |
| **S1** Нормализация | Номера реплик, время в секундах от начала, пометка повторов | [`cqa/conversation.py`](../../cqa/conversation.py) | — (в памяти) |
| **S2** Факты | Эскалация, первая реплика оператора, паузы, концовка, имя оператора | [`cqa/conversation.py`](../../cqa/conversation.py) | `runs/<run>/features/<id>.json` |
| **S3** Промпты | Шаблон + чек-лист + диалог + факты; варианты v1 / v2 / v3 | [`cqa/prompts.py`](../../cqa/prompts.py), [`prompts/templates/v1.md`](../../prompts/templates/v1.md) | `runs/<run>/prompts/*.md` |
| **S4** Суждение | **Вручную**: вставить промпт в чат агента, ответ — в файл | — | `runs/<run>/agents/<agent>/responses/*` |
| **S5** Валидация | Разбор JSON, допустимость ответа и N/A, ступени шкалы, существование реплик, совпадение цитат | [`cqa/responses.py`](../../cqa/responses.py) | `runs/<run>/validated/<agent>/<id>.json` |
| **S6** Агрегация | Большинство по агентам, согласие, расхождения; ансамбль как виртуальный агент `majority` | [`cqa/report.py`](../../cqa/report.py), [`cqa/evaluations.py`](../../cqa/evaluations.py) | `reports/majority.csv`, `validated/majority/` |
| **S7** Баллы | Формула чек-листа: веса, N/A, шкалы, округление half-up, флаг грубой ошибки | [`cqa/scoring.py`](../../cqa/scoring.py) | внутри `validated/…` |
| **S8** Вывод | Карточка: транскрипт, критерии с доказательствами, решения, итог, история | [`cqa/evaluations.py`](../../cqa/evaluations.py) | `conversation.card` |
| **S9** Эталон и метрики | Эталон с версиями и разбиением tune/test, точность с ДИ, κ, «Нет»↔N/A, MAE итога, доказательства, Макнемар, стоимость; согласие без эталона | [`cqa/groundtruth.py`](../../cqa/groundtruth.py), [`cqa/metrics.py`](../../cqa/metrics.py), [`cqa/report.py`](../../cqa/report.py) | `data/ground_truth/`, `history/reports/` |
| **S10** История и цикл | Журнал с цепочкой хешей, блобы, проекция «звезда»; апелляции A–D, прецеденты, выжимка, задачи оркестратора, предложения, регрессия, активация | [`cqa/events.py`](../../cqa/events.py), [`cqa/state.py`](../../cqa/state.py), [`cqa/db.py`](../../cqa/db.py), [`cqa/appeals.py`](../../cqa/appeals.py), [`cqa/loop.py`](../../cqa/loop.py), [`cqa/tasks.py`](../../cqa/tasks.py) | `history/` |

## Папка прогона

```
runs/<run_id>/
  manifest.json                 версии: чек-лист + хеш, шаблон, вариант, параметры + хеш, агенты; токены по каждому промпту; проекция v1/v2/v3
  features/<id>.json            факты S2
  prompts/<id>.md               канонические промпты
  agents/<agent>/               изолированный «почтовый ящик» агента
    INSTRUCTIONS.md             как вставлять (ручной режим) или как обработать папку (режим агента)
    prompts/<id>.md             копия промптов — агент не видит чужих ответов и эталона
    responses/<id>.md|json      сюда вставляется ответ модели целиком
    responses/<id>.usage.json   (необязательно) точный расход токенов, если интерфейс его показывает
  validated/<agent>/<id>.json   после ingest: ответы, проверки, баллы, токены
  reports/summary.md            отчёт; results.csv — длинная таблица; majority.csv — оценка большинства
```

Для вариантов v2 / v3 файлы называются `<id>__<секция или критерий>.md`, при разборе части собираются в одну оценку.

## Как работать (итерация 1)

```bash
python3 -m cqa prepare --run r001                       # все 65 диалогов DEV, вариант v1, все агенты
python3 -m cqa prepare --run r002 --sample 20 --variant v3   # стратифицированная выборка, по критерию
```

1. Для каждого агента открыть `runs/r001/agents/<agent>/prompts/`, вставить промпт в **новый** чат, ответ целиком сохранить в `responses/<то же имя>.md`.
   - Ускорение без API: открыть агента (Claude Code / Codex / Antigravity) прямо в папке `runs/r001/agents/<agent>/` и попросить выполнить `INSTRUCTIONS.md`.
2. Разобрать ответы — можно в любой момент, сколько бы ответов ни было:
   ```bash
   python3 -m cqa ingest --run r001
   python3 -m cqa report --run r001
   ```
3. Смотреть `runs/r001/reports/summary.md`: статус ответов, согласие по критериям, итоговые баллы, расхождения, токены.

## Чек-лист и версии

```bash
python3 -m cqa checklist list
python3 -m cqa checklist show default@1
python3 -m cqa checklist save --file my_edit.json --reason "уточнили условие N/A для c07" --author altay
python3 -m cqa checklist diff default@1 default@2
```

Сохранение создаёт новую версию (`v2`, `v3`…), старые не меняются и не удаляются. Каждый прогон фиксирует версию чек-листа и её хеш. Промпт собирается из версии автоматически — добавление, удаление или изменение критерия сразу попадает в промпты следующего `prepare`.

## Параметры

[`config/pipeline.json`](../../config/pipeline.json): агенты, соответствие агент → модель для цен, шаблон, передавать ли факты (`include_facts`), порог пауз (`gap_threshold_s`), порог совпадения цитат (`quote_match_threshold`). Цены — [`config/pricing.json`](../../config/pricing.json) (пока пустые).

## Токены

- Входные токены считаются при `prepare` для каждого промпта — и для выбранного варианта, и в проекции для всех трёх (v1 / v2 / v3).
- Выходные — по тексту сохранённого ответа.
- Метод — оценка: если установлен `tiktoken`, используется токенизатор `o200k_base`; иначе эвристика по символам. Точные значения — из `responses/<id>.usage.json`, если агент показал расход. Метод всегда указан в отчёте.

Проекция на DEV (65 диалогов, эвристика): v1 — 65 вызовов, ~249 тыс. входных токенов; v2 — 260 вызовов, ~812 тыс.; v3 — 520 вызовов, ~1,56 млн (×6,3 к v1).

## Расширенный ответ модели

Шаблон требует по каждому критерию: применимость (N/A), проверяемые подвопросы, все рассмотренные реплики «за» и «против», применённое правило и почему не соседняя ступень, неоднозначности, рассуждение, ответ, дословные цитаты, проверенный участок, объяснение в стиле чек-листа, уверенность. Плюс факты уровня диалога и собственный подсчёт итога моделью — чтобы проверить тезис, что считать итог должна формула, а не LLM.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_pipeline.py` покрывает расчёт баллов (100, перераспределение N/A, шкалы, округление half-up, флаг, секции), разбор и нормализация ответов, проверка реплик и цитат, факты, извлечение имени оператора. `tests/test_platform.py` — платформу целиком во временном рабочем каталоге (реальная история не трогается).

## Платформа: как пользоваться

```bash
python3 -m cqa commands                                   # все команды (= GET /api/v1/commands)
python3 -m cqa serve                                      # HTTP API для фронтенда
python3 -m cqa eval.aggregate run_id=r001                 # S6: ансамбль агентов
python3 -m cqa eval.import source=r001:majority           # оценки → история
python3 -m cqa eval.publish source=r001:majority          # официальные оценки
python3 -m cqa gt.draft 'sources=["r001:claude","r001:codex","r001:antigravity"]' set=dev-gold
python3 -m cqa gt.import path=data/ground_truth/dev-gold/draft.json set=dev-gold reason="разметка"
python3 -m cqa metrics.compare gold=dev-gold 'sources=["r001:claude","r001:majority"]' split=test
python3 -m cqa history.verify                             # цепочка хешей цела?
```

Ответ модели можно вставить и без файлов: `run.prompt` отдаёт текст промпта, `run.submit` сохраняет ответ. Провайдеры (ручной / CLI / API) — `config/providers.json`, отправка — `run.dispatch` (по кнопке).

## Чего пока нет

- HTTP-провайдер (API или локальная модель) — только контракт.
- Интерфейс (делает владелец проекта; контракт API — в [implementation.md](implementation.md)).
