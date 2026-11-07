# Реализация платформы

> Сессия реализации (Claude Code). Зона: `cqa/`, `scripts/`, `tests/`, `config/`, этот документ и [pipeline.md](pipeline.md). Решения команды 1–21 — в [discussion.md](discussion.md), параметры — в [design-space.md](design-space.md). Задание — [agent-briefs/claude-implementation.md](agent-briefs/claude-implementation.md).

**Коротко.** Пайплайн итерации 1 превращён в платформу на чистом Python 3.9 без зависимостей: журнал событий с цепочкой хешей как единственная правда, проекция в SQLite по схеме «звезда», версии всего (чек-лист, шаблоны, параметры, признаки, модели), эталон и метрики точности с ДИ и тестом Макнемара, контур апелляций A–D с прецедентами и слепым режимом, цикл обучения (выжимка → задача оркестратора → предложение → регрессия → активация человеком), аналитика по людям с n и интервалами, HTTP API из реестра команд (одна команда = одна кнопка MC), абстракция провайдеров LLM. Плюс эксперимент «субагенты вместо API»: сколько вызовов LLM на подзадачу и сколько это стоит.

## Запуск

```bash
python3 -m unittest discover -s tests          # 46 тестов: база + платформа + Antigravity
python3 -m cqa commands                         # контракт: все команды (то же, что GET /api/v1/commands)
python3 -m cqa serve                            # HTTP API на http://127.0.0.1:8765/api/v1/
python3 -m cqa <команда> key=value …            # любая команда из CLI, например:
python3 -m cqa conversation.card conversation_id=p1-ru-012
python3 -m cqa appeal.file evaluation_id=… criterion_id=c07 comment="…" actor=op-07
```

Классические команды итерации 1 (`prepare`, `ingest`, `report`, `checklist …`) работают как раньше.

## Архитектура

```
              ┌──────────── cqa/api.py: реестр команд (контракт) ────────────┐
CLI (cli.py) ─┤  одна команда = одна кнопка MC = один вызов CLI / HTTP         ├─ HTTP (server.py, CORS, JSON)
              └───────────────────────────────┬───────────────────────────────┘
                                              │ access.py — права ролей (решение 7)
  ┌─────────────┬───────────────┬─────────────┼──────────────┬──────────────┬───────────────┐
  run.py        evaluations.py  appeals.py    loop.py        groundtruth.py analytics.py    people.py
  S1–S7 (база)  импорт, S6,     A–D, решения, выжимка,       эталон,        по людям и      справочник,
  + провайдеры  карточка        прецеденты    предложения,   metrics.py     связям          операторы
                                              регрессия,     (S9)
                                              активация
  └─────────────┴───────────────┴─────────────┼──────────────┴──────────────┴───────────────┘
                                              ▼
          events.py: history/events.jsonl — только добавление, цепочка хешей; history/blobs/ — содержимое по хешу
                                              ▼
          state.py: текущее состояние = проигрывание журнала (итоговые оценки, прецеденты, статусы, HEAD версий)
                                              ▼
          db.py: history/cache/cqa.sqlite — схема «звезда», пересобирается из журнала; «машина времени» — в памяти
```

Принципы:

1. **Журнал — единственная правда (решение 8).** Ничего не перезаписывается: итоговая оценка, прецеденты, статусы апелляций, активная версия — вычисляются проигрыванием `history/events.jsonl`. SQLite — одноразовый кеш (`history/cache/`, не в git), пересобирается сам, когда журнал изменился.
2. **Цепочка хешей (VC.12).** Каждое событие содержит `prev` (хеш предыдущей строки) и `hash` (хеш себя). `python3 -m cqa history.verify` находит любую правку истории задним числом. Старые строки итерации 1 приняты как начало цепочки.
3. **Блобы по содержимому (VC.11, VC.14).** Полный результат оценки (с рассуждениями), снимки шаблонов и параметров, ответы оркестратора — `history/blobs/<hash>.json`; одинаковое содержимое хранится один раз; события ссылаются на хеш.
4. **«Машина времени» (VC.9).** Любая команда чтения принимает `until=<ISO-время>`: состояние проигрывается до этого момента, проекция строится в памяти. Карточка, аналитика, лента, версии — «как было тогда».
5. **Ничего автоматического (решение 11).** Каждое действие — команда; автоматические режимы — выключенные параметры в `config/platform.json` (`loop.auto_*`). Провайдеры CLI/API выключены.
6. **Ни одного критерия в коде (решения 3, 19).** Критерии — только из версии чек-листа; словари концовок, пороги, веса, цены — в `config/`.

## Модель данных

### Версии (решение 9)

| Что | Где | Как версионируется |
|---|---|---|
| Чек-лист | `data/checklists/<id>/v<N>.json` | неизменяемый снимок: автор, причина, родитель, хеш родителя, источник (предложение / ops). **HEAD** — событие `version_activated` (на профиль); до первой активации HEAD = v1 (чек-лист организаторов). Новая версия — черновик, пока человек не активирует. Архив — событие, файл остаётся |
| Критерий | внутри снимка | ревизии `c07@r1`, `c07@r2`: ревизия меняется, когда меняется **смысл** (всё, кроме `weight`, `section`, `status`) — смена веса не делает разметку эталона устаревшей |
| Шаблон промпта, параметры пайплайна, признаки S2, соответствие агент → модель, шаблоны задач оркестратора | блоб + событие `config_version_registered` | регистрируются автоматически при использовании (прогон, задача), без дублей; `versions.diff` — построчный / по ключам |
| Эталон | `data/ground_truth/<set>/v<N>.json` | как чек-лист: неизменяемые версии, автор, причина, хеш, фиксированное разбиение tune / test |

Каждая оценка несёт **полный набор версий** (VC.7): чек-лист + ревизии критериев, шаблон (версия, хеш), параметры, признаки (параметры + хеш кода `conversation.py`), модели, хеши кода подсчёта баллов и валидации, `run_id`. Это `manifest.versions` прогона, копируется в событие `evaluation_imported`.

### События журнала

| Группа | События |
|---|---|
| Прогоны и оценки | `run_prepared`, `response_submitted`, `run_dispatched`, `responses_ingested`, `report_built`, `evaluations_aggregated`, `evaluation_imported`, `evaluation_published` |
| Апелляции | `appeal_filed`, `appeal_assigned`, `appeal_opened`, `model_answer_revealed`, `decision_made`, `appeal_closed`, `precedent_marked`, `precedent_unmarked`, `reviewer_settings_changed` |
| Цикл обучения | `digest_built`, `task_created`, `task_dispatched`, `task_response_received`, `task_viewed`, `proposal_created`, `proposal_accepted`, `proposal_rejected`, `regression_run`, `metrics_computed` |
| Версии | `checklist_version_created`, `version_activated` (в т. ч. откат), `version_archived`, `version_restored`, `config_version_registered`, `config_changed` |
| Эталон, данные, люди | `gt_draft_created`, `gt_version_created`, `dataset_imported`, `person_registered`, `person_updated`, `operator_suggested`, `operator_assigned` |

Общие поля: `seq`, `id`, `ts`, `type`, `actor`, `prev`, `hash`; где уместно — `conversation_id`, `criterion_id` (для срезов ленты H.8).

### Проекция «звезда» (решение 10, DW.1–DW.3)

- **Измерения:** `dim_conversation` (канал, язык, длина, длительность, тип концовки, оператор), `dim_person`, `dim_checklist`, `dim_criterion` (ревизии), `dim_run`.
- **Факты:** `fact_evaluation` (исходный и итоговый балл, флаги, токены), `fact_criterion` (исходный и итоговый ответ и баллы по каждому критерию каждой оценки), `fact_appeal`, `fact_decision` (тип A–D, было → стало, время на решение, видел ли контраргумент, слепой режим, изменение итога), `fact_proposal`, `fact_task`, `fact_regression`, `feed` (лента истории).
- Любая связь — один `GROUP BY`: `python3 -m cqa db.query sql="SELECT …"` (только SELECT).

### Оценка, решение, итог

- Импорт (`eval.import source=r001:claude`) берёт результаты в формате базы — из `runs/`, из `variants/<имя>/runs/` или любой папки. Балл **пересчитывается** формулой платформы, файлу не доверяем.
- `eval.aggregate` — S6: ансамбль агентов прогона как виртуальный агент `majority` (большинство / медиана ступени, объяснение и доказательства — от агента с ответом большинства, доля согласия, пометка «нужна проверка человеком» при отсутствии большинства).
- `eval.publish` — какая оценка официальная (её видит оператор и считает аналитика по людям).
- Итог = исходные ответы + последнее решение проверяющего по каждому критерию → та же функция S7. Исходная оценка не меняется никогда.

## Контур апелляций (решения 4, 7)

| Шаг | Команда | Что проверяет платформа |
|---|---|---|
| Оспорить | `appeal.file` | критерий есть в версии оценки; комментарий обязателен; одна открытая апелляция на критерий; вложения — блобы |
| Назначить | `appeal.assign` | назначаемый — проверяющий |
| Открыть | `appeal.view` | тексты критерия и правил **той версии, по которой сделана оценка** (VC.8); фрагмент диалога вокруг доказательств; в слепом режиме ответ модели скрыт до `reveal=true` (факт раскрытия записывается) |
| Аргументы модели | `appeal.counterargument` | задача оркестратору; просмотр результата записывается — в решении поле `saw_counterargument` |
| Подсказка типа | `appeal.suggest_type` | задача оркестратору; тип выбирает человек |
| Решить | `appeal.decide` | только проверяющий / админ, **пайплайн и оператор — 403**; тип и действие согласованы: A ⇒ изменить, C и D ⇒ подтвердить, B — на усмотрение; новый ответ проверяется по типу критерия и допустимости N/A; повторное решение — только с `revise=true` поверх; аудит без апелляции (AP.7) — решение по `evaluation_id` + `criterion_id` |
| Прецедент | `precedent.mark` | только по кнопке (решение 11); `precedent.render` — блок для промпта (HX.6), отбор: тот же критерий + пересечение слов, k из конфигурации |
| Конфликты | `appeal.conflicts` | разные проверяющие — разные ответы по одному случаю; система показывает, не выбирает |

## Цикл обучения (S10)

```
loop.digest (алгоритм, проверяемый digest.json)
  → loop.analyze: задача оркестратору (prompt.md) → человек вставляет ответ → task.submit
  → proposal_created ×N (ops над базовой версией, ссылки на решения, проверка ops всухую;
    предложение веса без решений типа C получает предупреждение — решение 4)
  → proposal.preview: diff, затронутые апелляции и прецеденты, правила с упоминаниями, новые веса,
    устаревшая разметка эталона, сколько вызовов LLM на переоценку для V1 / V2 / V3 (CH.5, M.14)
  → proposal.accept (только админ, можно с исправленными ops) → новая версия-черновик
  → run.prepare на новой версии → ответы → regression.run (эталон test + все решения;
    принимается, если макро-точность не упала больше x п. п. и ни один прецедент не сломан)
  → version.activate (только админ; без пройденной регрессии — только force + причина; откат — активация старой)
```

Прямые правки человеком (HU.8) — `checklist.change ops=[…] save=true` (тот же предпросмотр). `checklist.request_edit` — правка обычными словами через оркестратора (K.7). `simulate.weights` — «что если» для весов: пересчёт всех итогов без единого вызова LLM, сдвиги категорий (решение 1, CL.E).

Шаблоны задач оркестратора — `config/task_templates/*.md` (резервные); если сессия дизайна положит `prompts/templates/task_<вид>.md`, используется он.

## Эталон и метрики (S9)

- **Формат метки:** `{"answer", "evidence_idx": [..], "comment", "source": consensus|human, "rev": "c07@r1"}`; набор — `data/ground_truth/<set>/v<N>.json` с разбиением `split.tune / split.test` (стратификация по префиксу id: язык и канал; `config/platform.json → ground_truth`).
- **Черновик (G.1):** `gt.draft sources=[r001:claude,r001:codex,r001:antigravity] set=dev-gold` — где агенты единогласны, метка заполнена как `consensus`; где нет — пустая, `needs_review`, со всеми кандидатами. Импорт (`gt.import`) не пропускает неразрешённые метки (или пропускает их с `allow_partial`). Есть импорт из CSV. Правка — `gt.update` → новая версия.
- **Метрики (`metrics.compare gold=dev-gold@v1 sources=[…] split=test`):** M.1 точность по критериям с n и интервалом Уилсона, M.2 макро и микро с бутстрэп-ДИ (ST.1), M.3 κ, M.4 матрица ошибок и отдельно «Нет»↔N/A, M.5–M.6 MAE / RMSE / смещение итога и доля в пределах ±5, M.7 P/R/F1 по грубым ошибкам, M.8 валидность доказательств (реплика есть, цитата совпадает), M.9 совпадение доказательств с эталоном (точное и ±1 реплика), M.11 невалидные, M.12 токены и $, ST.2 Макнемар для каждой пары источников, ST.3 разбивка по языку и каналу, список ошибок. Отчёт — `history/reports/metrics-*.md|json` + событие.
- **Без эталона:** `metrics.agreement` — стабильность повторных прогонов (M.10), согласие разметчиков (G.5), интерференция после смены чек-листа (M.15).
- Эталоном может быть и `decisions` — решения проверяющих как правда.
- Варианты Antigravity и Codex сравниваются лоб в лоб: `sources=[r001:majority, codex:pilot-review-v1:claude, r_anti_v3:claude]`.

## Аналитика по людям (SO, SR, RL, SP)

`analytics.operators | reviewers | pairs | criteria | model_errors by=language|channel|ending|criterion|agent | relate x=… y=… | overview`. Каждая доля — `{k, n, share, ci95}`; сравнение с командой — только при n ≥ `analytics.min_n_for_comparison`; ярлыков нет. Операторы: оценки, средний итог, грубые ошибки, апелляции и их доля, успешность, типы A–D, по критериям. Проверяющие: решения, время, доля изменений, согласие с моделью, направление (вверх / вниз), типы, влияние контраргумента, слепой режим против обычного. Пары оператор × проверяющий рядом с общей долей проверяющего.

Люди (решение 20): `people.seed_demo` — демо-справочник (помечен `demo`): `op-01…` по именам из чатов, `op-c1…op-c5` для звонков, `r-01…r-03`, `a-01`; оператор диалогу **предлагается** по имени из представления и подтверждается кнопкой (`people.assign`, `people.confirm_suggestions`).

## Контракт API (для фронтенда и Dynamics 365)

- `GET /api/v1/commands` — самоописание: имя, метод, MC, параметры. Фронтенд может строить формы по нему.
- Чтение — `GET /api/v1/<команда>?…`, любое изменение — только `POST` с JSON-телом (GET на изменяющую команду → 405).
- Ответ: `{"ok": true, "data": …, "events": ["e000123", …]}` — ид событий, добавленных этим действием (фронтенд может сразу показать запись в ленте). Ошибки: `{"ok": false, "error": {"code", "message"}}` с HTTP 400 / 403 / 404 / 409 / 501.
- Кто действует: `actor` в теле или заголовок `X-CQA-Actor`; роль — из справочника или префикса id. Авторизации нет (как требует задание), права ролей проверяются.
- **Под Dynamics 365:** без cookies и сессий, CORS (`config/platform.json → api.cors_origins`), все ид — строки, время — ISO 8601, ссылки на доказательства — `idx` реплики и `offset_s` от начала (для перемотки записи звонка). HTML-ресурс Dynamics может вызывать API по `fetch` из своего origin; карточка (`conversation.card`) отдаёт всё для одного экрана одним запросом.

### Все команды (сгенерировано из `cqa/api.py`)

Любая команда чтения принимает `until` (машина времени).

| Команда | Метод | MC | Что делает | Параметры (* — обязательный) |
|---|---|---|---|---|
| `commands` | GET |  | This table: every command with method, MC id and parameters | — |
| `whoami` | GET |  | Role of the actor (roles come from the people directory or the id prefix) | — |
| `history.feed` | GET |  | History table (H.7) with slices (H.8) | conversation_id, criterion_id, type, actor_id, limit, until |
| `history.verify` | GET |  | Check the hash chain of the event log (VC.12) | — |
| `history.at` | GET |  | Time machine (VC.9): active versions and counts at a moment | *until |
| `db.query` | GET |  | Read-only SQL over the star-schema projection | *sql, until |
| `export` | GET | MC.13 | Export history / evaluations / appeals / decisions as rows | *what, until |
| `dataset.list` | GET |  | Conversations of a dataset with operator and latest evaluation | dataset, until |
| `dataset.import` | POST | MC.1 | Upload transcripts in the organizers' API format | *dataset, *records |
| `conversation.card` | GET |  | Card: transcript, criteria with evidence, decisions, final, history | *conversation_id, evaluation_id, viewer, until |
| `people.list` | GET |  |  | role, until |
| `people.register` | POST |  |  | *person_id, *name, *role, team, languages |
| `people.update` | POST |  |  | *person_id, *fields |
| `people.seed_demo` | POST |  | Demo directory (marked demo) + operator suggestions by name (PE.5) | dataset |
| `people.assign` | POST | MC.2 | «Подтвердить оператора» for one conversation | *conversation_id, *operator_id |
| `people.confirm_suggestions` | POST | MC.2 | Confirm suggested operators (bulk button) | conversation_ids |
| `checklist.list` | GET |  |  | until |
| `checklist.get` | GET |  |  | ref |
| `checklist.diff` | GET | VC.10 |  | *a, *b |
| `checklist.revisions` | GET |  |  | checklist_id |
| `checklist.preview` | GET | MC.4 | Consequences of edit ops before saving (CH.5) | *ops, base |
| `checklist.change` | POST |  | Edit through the interface: ops → preview, or a new draft version (save=true) | *ops, reason, base, save |
| `checklist.save` | POST |  | Save a whole edited checklist document as the next version | *document, checklist_id, *reason |
| `version.activate` | POST | MC.11 | «Активировать» / «Откатить» (activate an older version) | *ref, reason, profile, force |
| `version.archive` | POST |  | Archive a version (nothing is deleted, VC.1) or restore it | *ref, *reason, restore |
| `versions.list` | GET |  | Registered versions of templates, params, features, models | kind, name, until |
| `versions.diff` | GET |  |  | *kind, *name, *a, *b |
| `params.get` | GET |  |  | — |
| `params.set` | POST |  | Change one pipeline parameter (new params version, logged) | *key, *value, *reason |
| `run.prepare` | POST | MC.3 | «Оценить»: build prompts (S1–S3) for a set of conversations | *run_id, dataset, checklist, variant, ids, sample, agents, template |
| `run.status` | GET |  |  | *run_id |
| `run.prompt` | GET |  | Text of one prompt to paste into a chat | *run_id, *name |
| `run.submit` | POST |  | Paste a model answer for one prompt (manual provider) | *run_id, *agent, *name, *text, usage |
| `run.dispatch` | POST | MC.3 | Send waiting prompts through the agent's provider (CLI/API when enabled) | *run_id, *agent, provider, limit |
| `run.ingest` | POST |  | S5–S7: validate pasted answers and score | *run_id, agents |
| `run.report` | POST |  | Agents head-to-head report of a run | *run_id |
| `eval.aggregate` | POST |  | S6: ensemble of the run's agents → validated/majority | *run_id, agents, name |
| `eval.import` | POST |  | Import results of a source (run:agent, variant:run:agent, folder) into the history | *source, ids |
| `eval.publish` | POST |  | Make evaluations official (what operators see and analytics count) | evaluation_ids, source |
| `eval.list` | GET |  |  | conversation_id, run_id, agent, until |
| `eval.final` | GET |  | Original, decisions and final of one evaluation | *evaluation_id, until |
| `appeal.file` | POST |  | «Оспорить»: criterion + comment | *evaluation_id, *criterion_id, *comment, operator_id, turn_idx, attachments |
| `appeal.assign` | POST | MC.5 |  | *appeal_id, *reviewer_id |
| `appeal.view` | POST |  | Reviewer's view: criterion text of the evaluation's version, comment, fragment, model answer (hidden in blind mode until reveal) | *appeal_id, reveal |
| `appeal.list` | GET |  |  | status, reviewer, operator, until |
| `appeal.decide` | POST |  | «Подтвердить» / «Изменить»: answer, reason, type A–D (ground truth) | appeal_id, evaluation_id, criterion_id, *action, new_answer, *reason, *type, overall_score, time_spent_s, revise |
| `appeal.close` | POST |  |  | *appeal_id, *reason |
| `appeal.conflicts` | GET |  | Different reviewers, different answers on the same case (HU.6) | until |
| `reviewer.settings` | POST | MC.12 | Blind mode switch (HU.5) | reviewer_id, blind |
| `appeal.counterargument` | POST | MC.6 | «Аргументы модели»: create an LLM task (manual provider) | *appeal_id, reviewer_reason |
| `appeal.suggest_type` | POST |  | Hint for type A–D by the orchestrator (AP.5), reviewer confirms | *appeal_id, *reviewer_reason |
| `precedent.mark` | POST | MC.7 | «Использовать как прецедент» (or on=false to withdraw) | *decision_id, note, on |
| `precedent.list` | GET |  |  | criterion_id, until |
| `precedent.render` | GET |  | Precedents block for a prompt (mechanism A, HX.6) | *conversation_id, criteria, dataset, k |
| `loop.digest` | POST | MC.8 | Algorithmic extract of the history (S10.2) — what the orchestrator sees | — |
| `loop.analyze` | POST | MC.8 | «Проанализировать историю»: digest → orchestrator task (proposals) | digest_id, provider |
| `checklist.request_edit` | POST |  | Change in plain words → orchestrator task → proposal (K.7) | *request, base |
| `task.list` | GET |  |  | kind, status, until |
| `task.prompt` | GET |  |  | *task_id |
| `task.submit` | POST |  | Paste the orchestrator's answer and parse it | *task_id, *text |
| `task.ingest` | POST |  | Parse a response file already saved next to the prompt | *task_id |
| `task.dispatch` | POST |  | Send a task through its provider (when not manual) | *task_id |
| `task.view` | POST |  | Open a task result (viewing a counter-argument is recorded, HU.4) | *task_id |
| `proposal.list` | GET |  |  | status, until |
| `proposal.create` | POST |  | A human proposal (same format as the orchestrator's) | *kind, *title, *ops, rationale, base, criterion_id, evidence_decisions |
| `proposal.preview` | GET |  | Consequences of a proposal if accepted | *proposal_id |
| `proposal.accept` | POST | MC.9 | «Принять» (optionally with edited ops) → new draft version | *proposal_id, reason, ops |
| `proposal.reject` | POST | MC.9 |  | *proposal_id, *reason |
| `regression.run` | POST | MC.10 | «Проверить на эталоне»: candidate vs baseline + decided appeals | *candidate, baseline, gold, split, include_decisions |
| `simulate.weights` | GET |  | What-if: totals with other weights / version, 0 LLM calls | weights, checklist, source, use_final |
| `gt.list` | GET |  |  | — |
| `gt.get` | GET |  |  | *set |
| `gt.draft` | POST |  | Draft labels from several agents: consensus pre-filled, disagreements for people | *sources, *set, ids, checklist |
| `gt.import` | POST |  | Import a reviewed draft / JSON / CSV as a new immutable version | *path, *set, *reason, allow_partial, checklist |
| `gt.update` | POST |  | Change labels → new version | *set, *labels, *reason |
| `metrics.compare` | POST |  | Accuracy of sources vs ground truth, head to head (+ report file) | *gold, *sources, split, save |
| `metrics.agreement` | GET |  | Agreement between sources: stability, annotators, interference | *sources, criteria |
| `sources.resolve` | GET |  |  | *source |
| `analytics.overview` | GET |  |  | until |
| `analytics.operators` | GET |  |  | until |
| `analytics.reviewers` | GET |  |  | until |
| `analytics.pairs` | GET |  |  | until |
| `analytics.criteria` | GET |  |  | until |
| `analytics.model_errors` | GET |  |  | by, until |
| `analytics.relate` | GET |  |  | *x, *y, until |
| `providers.list` | GET |  |  | — |

## Провайдеры LLM (L.3)

`cqa/providers.py`, `config/providers.json`. Любой шаг LLM — файл промпта на входе и файл ответа на выходе. Провайдер превращает одно в другое: `file` (ручной режим — сейчас), `command` (CLI: `claude -p`, `codex exec -`, `gemini -p`, обёртка локальной модели; выключены), `http` (API / локальный сервер — контракт есть, реализация — следующий шаг). Пайплайн не знает, кто записал ответ, поэтому переход на API не требует переделки. `run.dispatch` / `task.dispatch` — кнопка «отправить через провайдер». Для фронтенда ручного режима — `run.prompt` (текст для вставки) и `run.submit` (вставленный ответ).

## Субагенты вместо API: сколько вызовов LLM на подзадачу

**Зачем.** API и локальной модели пока нет (решения 14, 18). Вместо API-запроса — **изолированный субагент Claude Code на каждый промпт**: он читает ровно один файл промпта (копия вне репозитория), пишет ответ в файл и больше ничего не делает. Это даёт то же, что дал бы API: независимые вызовы, разные модели, любое число вызовов на диалог. Пайплайн не меняется — это просто ещё один «транспорт» файлового провайдера.

**Как воспроизвести.** `run.prepare` (варианты v1 / v2 / v3, агенты = имена моделей) → копии промптов в изолированную папку → оркестрация субагентов (по одному на промпт, модель и effort фиксированы) → `python3 scripts/experiment_calls.py --runs sa_v1 sa_v2 sa_v3 --box <папка>` (перенос ответов, разбор, оценка, отчёт). С эталоном — добавить `--gold <set>`.

**Пилот (5 диалогов × 8 критериев = 40 решений на конфигурацию, 140 вызовов, все ответы валидны).** Диалоги: `p1-ru-012` (концовка-ловушка), `p1-ru-022` (предложение денег лично), `p1-kz-008` (без представления), `p1-en-015`, звонок `p4-ru-001`. Один шаблон `v1` для всех вариантов — меняется только декомпозиция и модель. Effort субагентов — `medium`. Полный отчёт: [`runs/sa_v1/reports/experiment_calls.md`](../../runs/sa_v1/reports/experiment_calls.md).

| Конфигурация | Вызовов / диалог | $ / диалог (API) | $ / 65 диалогов | $ / 10 контрольных | Прокси-точность (95% ДИ) |
|---|---|---|---|---|---|
| V1 Haiku 5.5 | 1 | 0.0036 | 0.23 | 0.04 | 80.0% (60–95) |
| V2 Haiku 5.5 | 4 | 0.0074 | 0.48 | 0.07 | 75.0% (60–88) |
| V3 Haiku 5.5 | 8 | 0.0111 | 0.72 | 0.11 | 80.0% (75–85) |
| **V1 Sonnet 5.5** | **1** | **0.052** | **3.37** | **0.52** | **97.5% (92–100)** |
| V1 Sonnet 5.5, повтор | 1 | 0.055 | 3.55 | 0.55 | 95.0% (90–100) |
| V2 Sonnet 5.5 | 4 | 0.106 | 6.88 | 1.06 | 92.5% (88–98) |
| V3 Sonnet 5.5 | 8 | 0.167 | 10.86 | 1.67 | 92.5% (88–98) |
| V1 Opus 5.5 | 1 | 0.107 | 6.92 | 1.07 | 90.0% (82–98) |

Стоимость — эквивалент API по ценам Anthropic (вход / выход за 1 млн токенов: Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20; `config/pricing.json`), токены — оценка по тексту промпта и ответа. **Токены размышлений не видны, поэтому реальная стоимость выше** (для Sonnet и Opus — заметно).

**Прокси-точность** — доля ответов, совпавших с большинством *остальных семи* конфигураций (себя конфигурация не учитывает). Это мера согласия, а не правильности, и у неё есть смещение: среди «остальных» четыре конфигурации Sonnet, поэтому консенсус ближе к Sonnet, и Opus может быть занижен. Разница в 2,5 п. п. — это одно решение из 40. Настоящая точность — только против эталона людей: `python3 scripts/experiment_calls.py … --gold <set>`.

**Что видно уже сейчас:**

1. **Модель важнее декомпозиции.** Haiku держится на ~75–80% при любом числе вызовов; Sonnet — 92–97%. Переход Haiku → Sonnet даёт +15–20 п. п. согласия за +$0.05 на диалог (+$3 на весь DEV).
2. **Больше вызовов не дало прироста.** V2 и V3 у Sonnet не лучше V1 (−5 п. п., в пределах шума), а стоят в 2 и 3,2 раза дороже: на 65 диалогах $3.4 → $6.9 → $10.9. Для Haiku V3 в 3 раза дороже V1 при той же прокси-точности. В пилоте «по вызову на критерий» не окупается.
3. **Opus 5.5 не обогнал Sonnet** при двойной цене (с поправкой на смещение консенсуса — вывод предварительный).
4. **Стабильность Sonnet V1** — 97,5% совпадения двух независимых прогонов (1 расхождение из 40: `p1-kz-008 / c07`, «Нет» против N/A).
5. **Где расходятся все конфигурации** — ровно на спорных трактовках из обсуждения 0.2: шкала `c06` (4 / 7 / 10 — самый нестабильный критерий у всех), «Нет» ↔ N/A в `c02` и `c07`, ступень `c04` (15 / 20). Грубую ошибку `p1-ru-022` нашли все 8 конфигураций, ложных флагов нет.
6. **Одно решение «Нет» ↔ N/A двигает итог на ~16 баллов.** На `p1-ru-012` Sonnet V1 даёт 84.62, а Sonnet V2 / V3 — 68.75: единственная разница — `c02` («Нет» против N/A), который через перераспределение веса меняет весь итог. Разброс итога между конфигурациями — от 5.6 (`p1-ru-022`) до 37.7 баллов (`p1-ru-012`). Для метрики организаторов по итоговому баллу это главный риск.

**Итоговые баллы по конфигурациям:**

| Диалог | V1 Haiku | V1 Sonnet | V1 Sonnet (повтор) | V1 Opus | V2 Haiku | V2 Sonnet | V3 Haiku | V3 Sonnet | Разброс |
|---|---|---|---|---|---|---|---|---|---|
| p1-en-015 | 50.63 | 31.88 | 31.88 | 32.5 | 45.5 | 26.25 | 56.88 | 26.25 | 30.63 |
| p1-kz-008 | 46.92 | 38.13 | 40.67 | 38.13 | 56.88 | 38.13 | 56.88 | 38.13 | 18.75 |
| p1-ru-012 | 84.62 | 84.62 | 84.62 | 92.31 | 77.69 | 68.75 | 54.62 | 68.75 | 37.69 |
| p1-ru-022 | 25.63 | 20.0 | 20.0 | 20.0 | 25.63 | 20.0 | 25.63 | 20.0 | 5.63 |
| p4-ru-001 | 51.25 | 59.17 | 59.17 | 59.17 | 32.5 | 51.67 | 41.33 | 59.17 | 26.67 |

**Рекомендация (предварительная, до эталона).** База — **V1 на Sonnet 5.5** (~$0.05 за диалог, ~$0.5 за 10 контрольных). Бюджет, который ушёл бы на V3, лучше потратить точечно: (а) повторные прогоны V1 / ансамбль из 3 вызовов (~$0.16 за диалог — как V3) для стабильности; (б) второй, проверяющий вызов только по спорным критериям `c06`, `c02`, `c07`, `c04` — это V5 (направление Codex); (в) уточнения правил «Нет» ↔ N/A в `global_rules` — дешевле любого числа вызовов. Haiku — кандидат для дешёвой массовой предварительной оценки, но не для сдачи.

**Субагенты как замена API — что учесть:**

- **Изоляция неполная.** Субагент получает системный промпт Claude Code, git status и файл памяти проекта (описание хакатона). Ответов других моделей и эталона он не видит, но «знает», что это за проект — отличие от чистого API-вызова (фактор L.11).
- **Накладные расходы.** На 140 вызовов субагенты потратили ~9,4 млн токенов подписки против ~0,8 млн токенов эквивалента API (×12: системный промпт, схемы инструментов, чтение и запись файла). Для решения «сколько стоит в продакшене» используем эквивалент API; для бюджета подписки — фактический расход.
- **Надёжность.** 139 из 140 субагентов записали ответ сами; один вернул JSON текстом — ответ восстановлен из его транскрипта (его собственный ответ, без правок).
- **Время.** 140 вызовов при ~10 параллельных — ~12 минут.

**Следующий шаг.** Разметить эталон на этих 5 + 15 диалогах (`gt.draft` из трёх агентов → люди разрешают расхождения) и перезапустить `experiment_calls.py --gold` — тогда прокси заменится настоящей точностью, и можно честно выбрать модель и декомпозицию для контрольного прогона.

## Что сделано и чего нет

**Сделано:** всё из списка задания 1–8 в виде ядра и API с тестами (`tests/test_platform.py`: импорт и версии оценки, ансамбль, карточка, весь путь апелляции с правами и правилами типов, слепой режим, контраргумент, предложения → версия → активация и откат, предпросмотр замены критерия, симуляция весов, эталон → метрики, разбиение, подделка истории, машина времени, проекция и аналитика, CLI-провайдер, HTTP API с CORS / 403 / 405, правка чек-листа через API).

**Нет / ограничения:**
- HTTP-провайдер (API, локальная модель) — только контракт; включить — дописать `HttpProvider.complete`.
- Подразумеваемые веса регрессией (CL.E) — нет данных (общих оценок проверяющих); поле `overall_score` в решении уже собирается.
- Калибратор уверенности (A.6), подвопросы и V4/V5 — зона вариантов Antigravity и Codex; платформа принимает их результаты как источники.
- Прецеденты в промпт подставляются только если в шаблоне есть `{{PRECEDENTS}}` — см. предложения дизайну.
- Ид апелляций, решений, задач берутся по счётчику состояния: при одновременных POST от разных пользователей возможна гонка (для демо с одним пользователем не проявляется; лечится генерацией ид под блокировкой журнала).
- Проекция пересобирается целиком при каждом изменении журнала — на объёмах хакатона это доли секунды.

## Вопросы к дизайну

1. **Какую оценку публиковать для сдачи:** ансамбль (`majority`) или одну модель? Платформа поддерживает обе — нужен выбор и правило ничьей (A.3).
2. **HEAD чек-листа до первой активации = v1.** Подтвердить, что любая новая версия — черновик, пока её не активирует человек (сейчас так).
3. **Профили (K.4):** активную версию можно держать отдельно для `chat` и `voice call` — нужен ли отдельный профиль для звонков?
4. **Регрессия перед активацией обязательна** (можно обойти с причиной). Порог `max_macro_drop_pp = 0` — оставить?
5. **Эталон:** утвердить формат метки и порядок «черновик агентов → люди разрешают расхождения», размер и разбиение (сейчас 40 % в test).

## Предложения другим

- **Дизайну (`prompts/templates/`):** (а) добавить в шаблон плейсхолдер `{{PRECEDENTS}}` — платформа уже умеет отбирать и рендерить прецеденты (`precedent.render`); (б) забрать шаблоны задач оркестратора из `config/task_templates/` в `prompts/templates/task_<вид>.md` (платформа возьмёт их автоматически); (в) по результатам эксперимента с субагентами — см. раздел выше.
- **README (корень):** добавить строки запуска платформы: `python3 -m cqa serve`, `python3 -m cqa commands`, ссылку на этот документ.
- **`.gitignore`:** `history/cache/` уже игнорируется вложенным `history/.gitignore`; при желании перенести правило в корневой.
- **Antigravity и Codex:** результаты в формате `validated/<agent>/<id>.json` уже сравниваются платформой — `metrics.compare` / `metrics.agreement`, источник `variant:run:agent` или `run:agent`. Поле `checklist` в каждом результате или `manifest.json` рядом обязательны; `usage[].tokens_in/out` — для стоимости. Импорт в историю — `eval.import`.

## Идеи, которые усиливают точность, цикл и презентацию

1. **Эталон «агенты + люди» за час:** `gt.draft` по трём агентам заполняет единогласные метки; людям остаются только расхождения — это и есть список спорных трактовок (0.2). Сразу после — `metrics.compare` с ДИ.
2. **Демо замены критерия за минуту:** `checklist.change` с `replace_criterion` → предпросмотр (новые веса, «0 вызовов LLM для смены веса / 10 для V3 нового критерия», устаревшая разметка) → сохранить → `run.prepare` только на 10 контрольных диалогах.
3. **Демо цикла:** апелляция → решение типа B → прецедент → `loop.analyze` → предложение → `regression.run` (видно «исправлено: 3, сломано: 0, прецеденты целы») → активация. Каждый шаг — запись в ленте с цепочкой хешей.
4. **Аудит для финансовой компании:** `history.verify` — «подделка любой записи истории видна», плюс «машина времени» по любой карточке.
5. **Слайд «почему такая архитектура»:** таблица эксперимента ниже (вызовы × модель × $ × прокси-точность) и проекция на 65 и на 10 контрольных диалогов.
