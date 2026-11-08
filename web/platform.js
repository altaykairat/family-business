'use strict';
// Platform pages on top of app.js (shares its globals: state, api, act, header, icon, esc, number, toast, openCard,
// render, refOf, can, role). Pages: «Очередь проверки», «Цикл обучения», «Точность», «Оценка и сдача».

const page = {queue: null, loop: null, quality: null, runs: null};
const pageBusy = {};
let jobTimer = null;

const CRIT_TITLE = (id) => state.checklist?.criteria?.find((c) => c.id === id)?.title || id;
const pct = (v) => v == null ? '—' : `${number(v * 100)}%`;
const REASONS = {insufficient_data: 'модель отметила нехватку данных', ambiguities: 'модель отметила неоднозначность',
  'confidence<0.7': 'низкая уверенность модели', invalid_quote: 'цитата не найдена', disagreement: 'модели разошлись'};
const STEP = {queued: 'в очереди', prepare: 'сборка промптов', evaluate: 'оценка агентом', ingest: 'проверка ответов',
  publish: 'публикация', export: 'выгрузка results', finished: 'завершено'};
const JOB_STATUS = {running: 'идёт', done: 'готово', done_with_errors: 'готово с ошибками', failed: 'ошибка'};

const ANS = (v) => ({yes: 'Да', no: 'Нет', na: 'Н/П'})[v] ?? v;
const FEATURE = {channel: 'канал', ending: 'концовка', waits: 'ожидания', dissatisfaction: 'недовольство', language: 'язык'};
const FVAL = {'agent chat': 'чат', 'voice call': 'звонок', yes: 'есть', no: 'нет', 'last:operator': 'последним писал оператор', 'last:client': 'последним писал клиент',
  operator_closed: 'оператор закрыл чат', client_left: 'клиент ушёл', connection_lost: 'обрыв связи'};
const situation = (sit) => Object.entries(sit || {}).map(([k, v]) => `${FEATURE[k] || k}: ${FVAL[v] || v}`).join(', ') || 'любая';
function loading(title, sub) { return header(title, sub) + '<div class="loading">Загружаем данные…</div>'; }
function bar(value, max = 1, cls = '') { const w = max ? Math.max(0, Math.min(100, value / max * 100)) : 0; return `<span class="bar ${cls}"><i style="width:${w}%"></i></span>`; }

async function pageLoad(name) {
  if (!['queue', 'loop', 'quality', 'runs'].includes(name) || pageBusy[name]) return;
  pageBusy[name] = true;
  try {
    if (name === 'queue') {
      const q = await api('review.queue');
      page.queue = {queue: q, appeals: await api('appeal.list')};
    }
    if (name === 'loop') {
      const gold = page.loop?.gold ?? 'dev-gold';
      const win = page.loop?.win ?? 'since_active_version';
      const patterns = await api('loop.patterns', {...(gold ? {gold} : {}), window: win});
      const proposals = await api('proposal.list');
      const reports = await api('reports.list');
      const tasks = await api('task.list').catch(() => []);
      let ba = null;
      const last = reports.filter((r) => r.kind === 'before_after').pop();
      if (last) ba = {...await api('reports.get', {report_id: last.report_id}), report_id: last.report_id};
      page.loop = {gold, win, patterns, proposals, reports, tasks, ba, baList: reports.filter((r) => r.kind === 'before_after')};
    }
    if (name === 'quality') {
      const evals = await api('eval.list');
      const sources = [...new Set(evals.map((e) => `${e.run_id}:${e.agent}`))].filter((s) => !s.startsWith('rescore')).sort();
      const gt = await api('gt.list');
      const prev = page.quality || {};
      const source = prev.source && sources.includes(prev.source) ? prev.source : (sources.includes('r-loop2:codex-cli') ? 'r-loop2:codex-cli' : sources[0]);
      const gold = prev.gold || 'dev-gold@v1', split = prev.split || (source?.startsWith('r-loop') ? 'test' : 'all');
      let metrics = null, error = null;
      if (source) { try { metrics = await api('metrics.compare', {gold, sources: [source], split, save: false}); } catch (e) { error = e.message; } }
      page.quality = {sources, gt, source, gold, split, metrics, error};
    }
    if (name === 'runs') {
      const datasets = await api('dataset.names');
      const jobs = await api('job.status');
      const providers = await api('providers.list');
      page.runs = {...(page.runs || {}), datasets, jobs: jobs.jobs, providers: providers.providers.filter((p) => p.type === 'command' && p.enabled && p.available && !p.detach)};
      watchJobs();
    }
  } catch (e) { toast('Ошибка загрузки раздела: ' + e.message, true); }
  finally { pageBusy[name] = false; }
  if (state.page === name) render();
}
window.pageLoad = pageLoad;

// ------------------------------------------------------------------ queue
function queue() {
  const d = page.queue;
  if (!d) return loading('Очередь проверки', 'Ответы модели, которые стоит проверить человеку в первую очередь.');
  const openA = d.appeals.filter((a) => ['open', 'assigned'].includes(a.status));
  const prio = (i) => (i.reasons || []).includes('insufficient_data') * 10 + (i.reasons || []).length;
  const items = [...(d.queue.items || [])].sort((a, b) => prio(b) - prio(a));
  const byReason = d.queue.by_reason || {};
  return header('Очередь проверки', 'Ответы, где модель сама сомневается: нехватка данных, неоднозначность, низкая уверенность. Плюс апелляции, ожидающие решения.', `<button class="button" data-p="reload">${icon('refresh')} Обновить</button>`) +
    `<section class="stats">${stat('На проверку', number(items.length), `в ${number(d.queue.conversations || 0)} диалогах из ${number(d.queue.of || 0)}`, 'alert')}${Object.entries(byReason).slice(0, 2).map(([k, v]) => stat(REASONS[k] || k, number(v), 'ответов', 'score')).join('')}${stat('Апелляции ждут решения', number(openA.length), 'решение проверяющего окончательное', 'appeals')}</section>` +
    (openA.length ? `<div class="section-title"><h2>Апелляции <span>${openA.length}</span></h2></div><div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Апелляция</th><th>Диалог</th><th>Критерий</th><th>Оператор</th><th>Комментарий</th><th></th></tr></thead><tbody>${openA.map((a) => `<tr><td>${esc(a.appeal_id)}</td><td>${esc(a.conversation_id)}</td><td>${esc(a.criterion_id)} · ${esc(CRIT_TITLE(a.criterion_id))}</td><td>${esc(name(a.operator_id))}</td><td class="wrap">${esc(a.comment)}</td><td><button class="button primary" data-appeal="${esc(a.appeal_id)}">Решить</button></td></tr>`).join('')}</tbody></table></div></div>` : '') +
    `<div class="section-title"><h2>Ответы модели под вопросом <span>${items.length}</span></h2></div><div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Диалог</th><th>Критерий</th><th>Ответ модели</th><th>Почему в очереди</th><th></th></tr></thead><tbody>${items.slice(0, 80).map((i) => `<tr><td><button class="id-link" data-card="${esc(i.conversation_id)}">${esc(i.conversation_id)}</button></td><td>${esc(i.criterion_id)} · ${esc(CRIT_TITLE(i.criterion_id))}</td><td><b>${esc(ANS(i.answer))}</b></td><td class="wrap">${(i.reasons || []).map((r) => `<span class="pill amber">${esc(REASONS[r] || r)}</span>`).join(' ')}</td><td><button class="icon-button row-arrow" data-focus="${esc(i.conversation_id)}|${esc(i.evaluation_id)}|${esc(i.criterion_id)}" aria-label="Открыть">↗</button></td></tr>`).join('')}</tbody></table>${!items.length ? '<div class="empty">Очередь пуста — модель уверена во всех опубликованных ответах.</div>' : ''}</div></div>` +
    '<p class="subtitle" style="margin-top:18px">Очередь строится из сигналов самой модели и проверки ответа. Проверяющий открывает диалог на нужном критерии; изменить оценку можно через апелляцию.</p>';
}

// ------------------------------------------------------------------ loop
function loop() {
  const d = page.loop;
  if (!d) return loading('Цикл обучения', 'Решения проверяющих → закономерности → предложения правил → проверка → новая версия.');
  const ready = d.patterns.ready || [], acc = d.patterns.accumulating || [], th = d.patterns.thresholds || {};
  const open = d.proposals.filter((p) => p.status === 'open'), done = d.proposals.filter((p) => p.status !== 'open');
  const regressions = d.reports.filter((r) => r.kind === 'regression');
  const winSel = `<select class="filter-select" data-p-change="loop-win"><option value="since_active_version" ${d.win !== 'all' ? 'selected' : ''}>С активации ${esc(d.patterns.window?.active || '')}</option><option value="all" ${d.win === 'all' ? 'selected' : ''}>Вся история решений</option></select>`;
  const goldSel = `<select class="filter-select" data-p-change="loop-gold">${[['dev-gold', 'Эталон: основной'], ['dev-gold-strict', 'Эталон: строгий c07'], ['', 'Все решения']].map(([v, t]) => `<option value="${v}" ${d.gold === v ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
  const steps = [['1', 'Решения проверяющих', 'апелляции и их тип A–D'], ['2', 'Закономерности', 'одна апелляция — случай, серия — правило'], ['3', 'Предложения оркестратора', 'Codex предлагает, человек решает'], ['4', 'Регрессия', 'проверка на отложенных диалогах'], ['5', 'Активация', 'новая версия чек-листа']];
  let h = header('Цикл обучения', 'Система учится на решениях проверяющих. Правило появляется только из повторяющихся решений, принимает его человек, проверка — на диалогах, которые не участвовали в настройке.',
    `${winSel}${goldSel}<button class="button" data-p="reload">${icon('refresh')} Обновить</button>`) +
    `<div class="loop-steps">${steps.map(([n, t, s]) => `<div class="loop-step"><span>${n}</span><b>${t}</b><small>${s}</small></div>`).join('')}</div>`;

  if (d.ba) {
    const s = d.ba.summary;
    h += `<section class="panel"><div class="panel-head"><h2>До и после цикла</h2><span class="micro">${esc(d.ba.before)} → ${esc(d.ba.after)} · ${esc(d.ba.gold)} · ${d.ba.conversations} отложенных диалогов</span></div>
      ${d.baList.length > 1 ? `<div class="tabs">${d.baList.map((r) => `<button class="tab ${r.report_id === d.ba.report_id ? 'active' : ''}" data-p="ba" data-id="${esc(r.report_id)}">${esc(r.after)}</button>`).join('')}</div>` : ''}
      <div class="ba-grid">${baStat('Совпадение с эталоном', s.micro.before, s.micro.after, true)}${baStat('Ошибка итога (MAE)', s.mae_total.before, s.mae_total.after, false, 'балла')}${baStat('Итог в пределах ±5', s.within_tolerance.before, s.within_tolerance.after, true)}<div class="ba-stat"><span>Исправлено / сломано</span><b class="good">+${s.fixed}</b> <b class="bad">−${s.broken}</b><small>McNemar p = ${number(s.mcnemar_p)}</small></div></div>
      <div class="table-scroll"><table class="ba-table"><thead><tr><th>Критерий</th><th>До</th><th>После</th><th></th><th>+ / −</th></tr></thead><tbody>${Object.entries(d.ba.criteria).map(([k, v]) => `<tr><td>${esc(k)} · ${esc(v.title)}</td><td>${pct(v.before)}</td><td><b>${pct(v.after)}</b></td><td class="delta ${v.after > v.before ? 'good' : v.after < v.before ? 'bad' : ''}">${v.after === v.before ? '·' : (v.after > v.before ? '▲ ' : '▼ ') + number(Math.abs(v.after - v.before) * 100) + ' п. п.'}</td><td>${v.fixed ? `<span class="good">+${v.fixed}</span>` : ''} ${v.broken ? `<span class="bad">−${v.broken}</span>` : ''}</td></tr>`).join('')}</tbody></table></div></section>`;
  }

  h += `<div class="section-title"><h2>Закономерности <span>${ready.length} готово · ${acc.length} копится</span></h2>${can.editChecklist() ? `<span class="actions"><button class="button" data-p="sim">Симулировать проверяющего</button><button class="button primary" data-p="analyze">${icon('play')} Проанализировать историю</button></span>` : ''}</div>
    <p class="subtitle">Порог правила: случаев ≥ ${esc(th.min_cases)}, разных проверяющих ≥ ${esc(th.min_reviewers)}, противоречий ≤ ${number((th.max_contradiction_share || 0) * 100)}%, нижняя граница доли согласных ≥ ${number((th.min_wilson_lower || 0) * 100)}%. Ниже порога — «копится»: одно решение не меняет систему.</p>
    ${d.win !== 'all' && !ready.length && !acc.length ? `<div class="action-box">С активации ${esc(d.patterns.window?.active || '')} новых решений проверяющих нет: прошлые решения уже учтены в этой версии, цикл начинается заново. Переключите «Вся история решений», чтобы увидеть, из чего она выросла.</div>` : ''}
    <div id="loop-action"></div>
    <div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Критерий</th><th>Было → стало</th><th>Ситуация</th><th>Случаев</th><th>Проверяющих</th><th>Противоречий</th><th>Статус</th></tr></thead><tbody>${[...ready.map((p) => [p, true]), ...acc.slice(0, 12).map((p) => [p, false])].map(([p, ok]) => { const sg = p.signature || {}; const sit = situation(sg.situation); return `<tr><td>${esc(sg.criterion_id)} · ${esc(CRIT_TITLE(sg.criterion_id))}</td><td><b>${esc(ANS(sg.was))}</b> → <b>${esc(ANS(sg.became))}</b></td><td>${esc(sit)}</td><td>${p.cases}</td><td>${(p.reviewers || []).length}</td><td>${p.contradictions}</td><td>${ok ? '<span class="pill">готово к правилу</span>' : '<span class="pill neutral">копится</span>'}</td></tr>`; }).join('')}</tbody></table>${!ready.length && !acc.length ? '<div class="empty">Решений проверяющих пока нет.</div>' : ''}</div></div>`;

  h += `<div class="section-title"><h2>Предложения <span>${open.length} ждут решения</span></h2></div>` + (open.length ? open.map(proposalCard).join('') : '<div class="panel"><p class="subtitle">Открытых предложений нет. Нажмите «Проанализировать историю», когда закономерности готовы.</p></div>');
  if (done.length) h += `<details class="panel done-list"><summary>Решённые предложения (${done.length})</summary>${done.map((p) => `<p><b>${esc(p.proposal_id)}</b> · ${esc(p.criterion_id || '')} · ${esc(p.title)} — <span class="pill ${p.status === 'accepted' ? '' : 'neutral'}">${esc({accepted: 'принято', rejected: 'отклонено'}[p.status] || p.status)}</span>${p.resulting_version ? ` → ${esc(p.resulting_version)}` : ''}</p>`).join('')}</details>`;
  if (regressions.length) h += `<div class="section-title"><h2>Регрессионные проверки <span>${regressions.length}</span></h2></div><div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Проверка</th><th>Версия</th><th>Кандидат</th><th>База</th><th>Результат</th></tr></thead><tbody>${regressions.map((r) => `<tr><td>${esc(r.report_id.replace('regression-', ''))}</td><td>${esc(r.checklist_ref)}</td><td>${esc(r.candidate)}</td><td>${esc(r.baseline)}</td><td>${r.passed ? '<span class="pill">пройдена</span>' : '<span class="pill red">не пройдена</span>'}</td></tr>`).join('')}</tbody></table></div></div>`;
  return h;
}
function baStat(title, before, after, isShare, unit = '') {
  const better = isShare ? after > before : after < before;
  const fmt = (v) => isShare ? pct(v) : number(v);
  return `<div class="ba-stat"><span>${title}</span><b>${fmt(after)}</b><small class="${after === before ? '' : better ? 'good' : 'bad'}">было ${fmt(before)}${unit ? ' ' + unit : ''}</small></div>`;
}
function proposalCard(p) {
  return `<article class="panel proposal"><div class="panel-head"><h2>${esc(p.proposal_id)} · ${esc(p.title)}</h2><span class="pill neutral">${esc(p.kind)}${p.criterion_id ? ' · ' + esc(p.criterion_id) : ''}</span></div>
    <p>${esc(p.rationale || '')}</p>${(p.ops || []).map((op) => `<div class="op"><b>${esc({add_clarification: 'Уточнение правила', edit_clarification: 'Правка уточнения', add_note: 'Прецедент / заметка', set_weight: 'Изменение веса', set: 'Изменение'}[op.op] || op.op)}</b>${op.value?.text ? `<p>${esc(op.value.text)}</p>` : typeof op.value === 'string' ? `<p>${esc(op.value)}</p>` : op.value != null ? `<p>${esc(JSON.stringify(op.value))}</p>` : ''}</div>`).join('')}
    <p class="meta">Основано на решениях: ${(p.evidence_decisions || []).map(esc).join(', ') || '—'} · источник: ${esc(p.source || 'оркестратор')}</p>
    ${can.editChecklist() ? `<div class="actions"><button class="button" data-p="reject" data-id="${esc(p.proposal_id)}">Отклонить</button><button class="button primary" data-p="accept" data-id="${esc(p.proposal_id)}">Принять → новая версия</button></div>` : '<p class="meta">Принимает или отклоняет администратор.</p>'}</article>`;
}

// ------------------------------------------------------------------ quality
function quality() {
  const d = page.quality;
  if (!d) return loading('Точность', 'Сравнение оценок системы с эталоном.');
  const golds = d.gt.flatMap((g) => g.versions.map((v) => `${g.set_id}@v${v}`));
  const ctrl = `<select class="filter-select" data-p-change="q-source">${d.sources.map((s) => `<option ${s === d.source ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select><select class="filter-select" data-p-change="q-gold">${golds.map((g) => `<option ${g === d.gold ? 'selected' : ''}>${esc(g)}</option>`).join('')}</select><select class="filter-select" data-p-change="q-split">${[['all', 'Все диалоги'], ['test', 'Отложенные (test)'], ['tune', 'Настройка (tune)']].map(([v, t]) => `<option value="${v}" ${v === d.split ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
  let h = header('Точность', 'Насколько оценки системы совпадают с эталоном. Эталон — наша разметка DEV (решение 22), приближение к эталону организаторов; честнее всего сравнивать варианты между собой.', ctrl);
  if (d.error) return h + `<div class="error">${esc(d.error)}</div>`;
  const x = d.metrics?.sources?.[0];
  if (!x) return h + '<div class="empty">Нет оценок для сравнения.</div>';
  const t = x.totals, flag = x.criteria.c08?.flag;
  h += `<section class="stats">${stat('Совпадение меток', pct(x.micro_accuracy.share), `${x.micro_accuracy.k} из ${x.micro_accuracy.n} · ДИ ${pct(x.micro_accuracy.ci95?.[0])}–${pct(x.micro_accuracy.ci95?.[1])}`, 'score', true)}${stat('Ошибка итога (MAE)', `${number(t.mae)} <small>балла</small>`, `итог в пределах ±5: ${t.within_tolerance.k} из ${t.within_tolerance.n}`, 'chat')}${stat('Грубые ошибки', flag ? `${number(flag.recall * 100)}%` : '—', flag ? `найдено ${flag.tp} из ${flag.tp + flag.fn} · ложных ${flag.fp}` : '', 'alert')}${stat('Доказательства', pct(x.evidence?.quote_ok?.share), `цитаты дословны · совпадение с эталоном ${pct(x.evidence?.match_gold_window?.share)}`, 'checklist')}</section>`;
  h += `<section class="panel"><div class="panel-head"><h2>По критериям</h2><span class="micro">${esc(d.source)} против ${esc(d.gold)} · ${esc({all: 'все диалоги', test: 'отложенные', tune: 'настройка'}[d.split])} · ${x.coverage?.predicted ?? ''} диалогов</span></div>${Object.entries(x.criteria).map(([k, v]) => `<div class="crit-row"><span class="crit-name">${esc(k)} · ${esc(v.title)}</span>${bar(v.share, 1, v.share < 0.75 ? 'warn' : '')}<b>${pct(v.share)}</b><small>${v.k}/${v.n}${v.kappa != null ? ` · κ ${number(v.kappa)}` : ''}</small></div>`).join('')}
    <p class="meta">«Нет» вместо N/A: ${x.no_na_confusion.gold_na_pred_no} · N/A вместо «Нет»: ${x.no_na_confusion.gold_no_pred_na}. Одно такое расхождение сдвигает итог примерно на 16 баллов.</p></section>`;
  const errs = (x.errors || []).slice(0, 30);
  if (errs.length) h += `<div class="section-title"><h2>Расхождения с эталоном <span>${(x.errors || []).length}</span></h2></div><div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Диалог</th><th>Критерий</th><th>Эталон</th><th>Система</th><th></th></tr></thead><tbody>${errs.map((e) => `<tr><td>${esc(e.conversation_id)}</td><td>${esc(e.criterion_id)} · ${esc(CRIT_TITLE(e.criterion_id))}</td><td><b>${esc(ANS(e.gold))}</b></td><td>${esc(ANS(e.pred))}</td><td><button class="icon-button row-arrow" data-card="${esc(e.conversation_id)}">↗</button></td></tr>`).join('')}</tbody></table></div></div>`;
  return h;
}

// ------------------------------------------------------------------ runs
function runs() {
  const d = page.runs;
  if (!d) return loading('Оценка и сдача', 'Загрузка наборов, оценка агентом, выгрузка результатов.');
  const ds = d.datasets.map((x) => `<option value="${esc(x.dataset)}">${esc(x.dataset)} · ${x.conversations} диалогов</option>`).join('');
  const provs = d.providers.map((p) => `<option value="${esc(p.name)}">${esc(p.label)}</option>`).join('');
  let h = header('Оценка и сдача', 'Новые диалоги → оценка агентом по активному чек-листу → публикация → файл results для организаторов. Длинные задания идут в фоне.', `<button class="button" data-p="reload">${icon('refresh')} Обновить</button>`);
  h += `<section class="analysis-grid"><div class="panel"><div class="panel-head"><h2>1 · Загрузить диалоги</h2><span class="micro">формат API организаторов</span></div>
      <div class="form-grid"><label class="field">group_id набора<input id="r-group" placeholder="например CONTROL"></label><label class="field">Локальное имя<input id="r-name" placeholder="prod"></label></div>
      <div class="actions"><button class="button primary" data-p="fetch">Загрузить из API</button><label class="button">Импорт JSON<input id="r-file" type="file" accept=".json,application/json" hidden></label></div>
      <p class="meta">Адрес API берётся из .env (CQA_API_URL). Наборы: ${d.datasets.map((x) => `<b>${esc(x.dataset)}</b> (${x.conversations})`).join(', ')}</p></div>
    <div class="panel"><div class="panel-head"><h2>2 · Оценить набор</h2><span class="micro">активный чек-лист ${esc(state.versions.find((v) => v.status === 'active')?.ref || '')}</span></div>
      <div class="form-grid"><label class="field">Набор<select id="r-dataset">${ds}</select></label><label class="field">Агент<select id="r-provider">${provs}</select></label>
      <label class="field">Параллельно сессий<input id="r-workers" type="number" min="1" max="8" value="4"></label><label class="field">После оценки выгрузить<select id="r-export"><option value="">не выгружать</option><option value="dev">results/dev</option><option value="prod">results/prod</option></select></label>
      <label class="field wide">Только эти диалоги (через пробел, пусто — весь набор)<input id="r-ids" placeholder="p1-ru-001 p1-ru-002"></label></div>
      <div class="actions"><button class="button primary" data-p="evaluate">${icon('play')} Оценить в фоне</button></div>
      <p class="meta">Каждый диалог — новая сессия агента без контекста. ≈ 2–3 минуты на диалог; 10 диалогов при 4 сессиях ≈ 6–8 минут.</p></div></section>`;
  h += `<div class="section-title"><h2>Задания <span>${d.jobs.length}</span></h2></div>` + (d.jobs.length ? d.jobs.map(jobCard).join('') : '<div class="panel"><p class="subtitle">Заданий пока нет.</p></div>');
  h += `<section class="panel"><div class="panel-head"><h2>3 · Файлы для организаторов</h2><span class="micro">results/&lt;набор&gt;/results.json · формат JSON 1.0</span></div>
    <div class="form-grid"><label class="field">Набор<select id="x-dataset">${ds}</select></label><label class="field">Папка<select id="x-set"><option value="dev">results/dev</option><option value="prod">results/prod</option></select></label></div>
    <div class="actions"><button class="button primary" data-p="export">Выгрузить и проверить</button><button class="button" data-p="check">Проверить существующий файл</button></div><div id="x-result">${d.lastExport ? exportView(d.lastExport) : ''}</div>
    <p class="meta">Выгружаются опубликованные оценки с учётом решений проверяющих. Проверка: UTF-8, схема, 8 критериев ровно по разу, типы значений, уникальные ID, число записей. После выгрузки — commit и push до стоп-кода.</p></section>`;
  return h;
}
function jobCard(j) {
  const p = j.params || {}, r = j.result;
  return `<article class="panel job"><div class="panel-head"><h2>${esc(j.job_id)} · ${esc(p.dataset)}${p.ids ? ` (${p.ids.length})` : ''}</h2><span class="pill ${j.status === 'running' ? 'amber' : j.status === 'failed' ? 'red' : ''}">${esc(JOB_STATUS[j.status] || j.status)}</span></div>
    <div class="job-progress">${bar(j.done, j.total || 1)}<b>${j.done} / ${j.total || '…'}</b><span class="meta">${esc(STEP[j.step] || j.step)} · ${number(j.seconds)} с · ${esc(p.provider)} · ${esc(p.run_id)}</span></div>
    ${j.failed?.length ? `<div class="error">Не оценены: ${j.failed.map((f) => `${esc(f.name)} (${esc(f.error || f.status)})`).join('; ')}</div>` : ''}
    ${j.error ? `<div class="error">${esc(j.error)}</div>` : ''}
    ${r ? `<p class="meta">Проверено: ok ${r.validated?.ok ?? 0}, предупреждений ${r.validated?.warn ?? 0}, ошибок ${r.validated?.error ?? 0} · загружено ${r.imported ?? 0}${r.published ? ` · опубликовано ${r.published}` : ''}${r.export ? ` · ${esc(r.export.path)} — проверка ${r.export.check.ok ? 'пройдена' : 'НЕ пройдена'}` : ''}</p>` : ''}
    <details class="sub"><summary>Журнал</summary><pre>${esc((j.log || []).join('\n'))}</pre></details></article>`;
}
function exportView(x) {
  const c = x.check || x;
  return `<div class="${c.ok ? 'export-ok' : 'error'}">${c.ok ? '✓' : '✗'} ${esc(x.path || c.path || '')} · записей ${c.counts?.records ?? '—'} · ok ${c.counts?.ok ?? '—'} · ошибок ${c.counts?.error ?? '—'}${(c.errors || []).length ? `<br>${c.errors.slice(0, 5).map(esc).join('<br>')}` : ''}</div>`;
}
function watchJobs() {
  clearInterval(jobTimer);
  if (!(page.runs?.jobs || []).some((j) => j.status === 'running')) return;
  jobTimer = setInterval(async () => {
    try {
      const jobs = (await api('job.status')).jobs;
      const wasRunning = (page.runs.jobs || []).filter((j) => j.status === 'running').map((j) => j.job_id);
      page.runs.jobs = jobs;
      if (state.page === 'runs') render();
      const finished = jobs.filter((j) => wasRunning.includes(j.job_id) && j.status !== 'running');
      if (finished.length) { toast(`Задание ${finished.map((j) => j.job_id).join(', ')}: ${JOB_STATUS[finished[0].status]}`); load(); }
      if (!jobs.some((j) => j.status === 'running')) clearInterval(jobTimer);
    } catch { /* keep polling */ }
  }, 4000);
}

// ------------------------------------------------------------------ actions
function onPlatformAction(b) {
  const p = b.dataset.p;
  if (b.dataset.focus) { const [conv, ev, crit] = b.dataset.focus.split('|'); openCard(conv, ev, crit); return true; }
  if (!p) return false;
  const reload = (name) => { page[name] = page[name] ? {...page[name]} : null; return pageLoad(name); };
  if (p === 'reload') { pageLoad(state.page); return true; }
  if (p === 'ba') act(async () => { page.loop.ba = {...await api('reports.get', {report_id: b.dataset.id}), report_id: b.dataset.id}; render(); });
  if (p === 'sim') {
    act(async () => {
      const source = prompt('Источник оценок для симуляции (прогон:агент):', 'r-base:codex-cli'); if (!source) return;
      const gold = page.loop.gold ? `${page.loop.gold}@v1` : 'dev-gold@v1';
      const r = await api('sim.appeals', {gold, source, split: 'tune', reviewers: ['r-01', 'r-02', 'r-03']});
      toast(`Симулированный проверяющий: ${r.stats.appeals} апелляций и решений по ${r.stats.conversations} диалогам (эталон ${gold})`);
      await reload('loop');
    });
  }
  if (p === 'analyze') {
    act(async () => {
      $('#loop-action').innerHTML = '<div class="action-box"><b class="running">Оркестратор (Codex, новая сессия) анализирует историю решений… обычно около минуты.</b></div>';
      const gold = page.loop.gold || undefined;
      const r = await api('loop.analyze', {provider: 'codex-cli', ...(gold ? {gold} : {})});
      if (!r.task) { $('#loop-action').innerHTML = `<div class="action-box">Закономерностей выше порога нет — всё копится. Оркестратор не вызывался.</div>`; return; }
      const res = await api('task.dispatch', {task_id: r.task.task_id});
      const ing = res.ingest?.result;
      $('#loop-action').innerHTML = res.status === 'done' ? `<div class="action-box"><b>Оркестратор ответил за ${number(res.seconds)} с:</b> ${ing?.proposals?.length || 0} предложений, ${ing?.conflicts?.length || 0} конфликтов.${(ing?.conflicts || []).map((c) => `<p class="meta">Конфликт: ${esc(c.description)}</p>`).join('')}${ing?.notes ? `<p class="meta">${esc(ing.notes)}</p>` : ''}</div>` : `<div class="error">Оркестратор не ответил: ${esc(res.error || res.status)}</div>`;
      const keep = $('#loop-action').innerHTML;
      await reload('loop');
      if ($('#loop-action')) $('#loop-action').innerHTML = keep;
    });
  }
  if (p === 'accept') act(async () => { const reason = prompt('Причина принятия (попадёт в историю):', 'Принято администратором'); if (!reason) return; const r = await api('proposal.accept', {proposal_id: b.dataset.id, reason}); toast(`Принято → ${r.new_version} (черновик). Активируйте в разделе «Чек-лист» после регрессии.`); await load(); await reload('loop'); });
  if (p === 'reject') act(async () => { const reason = prompt('Причина отклонения:'); if (!reason) return; await api('proposal.reject', {proposal_id: b.dataset.id, reason}); toast('Предложение отклонено'); await reload('loop'); });
  if (p === 'fetch') act(async () => { const g = $('#r-group').value.trim(); if (!g) throw Error('укажите group_id'); const r = await api('dataset.fetch', {group_id: g, dataset: $('#r-name').value.trim() || undefined}); toast(`Загружено диалогов: ${r.imported.length} → набор ${r.dataset}`); await reload('runs'); });
  if (p === 'evaluate') {
    act(async () => {
      const ids = $('#r-ids').value.split(/[\s,]+/).filter(Boolean);
      const j = await api('job.evaluate', {dataset: $('#r-dataset').value, provider: $('#r-provider').value, workers: Number($('#r-workers').value) || 4, publish: true, ...(ids.length ? {ids} : {}), ...($('#r-export').value ? {export_set: $('#r-export').value} : {})});
      toast(`Запущено ${j.job_id}: прогресс — в списке заданий`);
      await reload('runs');
    });
  }
  if (p === 'export') act(async () => { const r = await api('results.export', {set: $('#x-set').value, dataset: $('#x-dataset').value}); page.runs.lastExport = r; render(); toast(r.check.ok ? `Выгружено ${r.path}, проверка пройдена` : 'Выгружено, но проверка не пройдена', !r.check.ok); });
  if (p === 'check') act(async () => { const set = $('#x-set').value; const r = await api('results.check', {path: `results/${set}/results.json`, set}); page.runs.lastExport = {path: `results/${set}/results.json`, check: r}; render(); });
  return true;
}
window.onPlatformAction = onPlatformAction;

document.addEventListener('change', (e) => {
  const k = e.target.dataset?.pChange;
  if (!k) {
    if (e.target.id === 'r-file') act(async () => {
      const f = e.target.files[0]; if (!f) return;
      const data = JSON.parse(await f.text());
      const records = data?.data?.records || data.records || (Array.isArray(data) ? data : [data]);
      const dataset = $('#r-name').value.trim() || data?.data?.dataset_group_id?.toLowerCase() || 'upload';
      const r = await api('dataset.import', {dataset, records});
      toast(`Импортировано диалогов: ${r.imported.length} → набор ${r.dataset}`);
      await pageLoad('runs');
    });
    return;
  }
  if (k === 'loop-gold') { page.loop.gold = e.target.value; pageLoad('loop'); }
  if (k === 'loop-win') { page.loop.win = e.target.value; pageLoad('loop'); }
  if (k.startsWith('q-')) { page.quality[k.slice(2)] = e.target.value; pageLoad('quality'); }
});

window.PAGES = {queue, loop, quality, runs};

// app.js routes before this file loads: load the current page now if it is one of ours.
if (page[state.page] === null) pageLoad(state.page);
