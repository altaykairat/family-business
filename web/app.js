'use strict';
// Freedom Quality — UI over the CQA platform API (/api/v1/<command>).
// Design and page structure: the team's new site. Actions (roles, appeals, decisions, manual evaluation,
// checklist versions, trace) are platform commands; permissions are checked by the server by the actor id.

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const paths = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chat: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2a8.5 8.5 0 0 1 8.5 8.5Z"/><path d="M7 9h9M7 13h6"/>',
  operators: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5"/>',
  appeals: '<path d="M4 4h16v12H9l-5 4V4Z"/><path d="M12 7v4M12 13v1"/>',
  checklist: '<rect x="5" y="4" width="15" height="18" rx="2"/><path d="M9 2h7v4H9zM9 11l1 1 2-2M14 11h3M9 16l1 1 2-2M14 16h3"/>',
  export: '<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  call: '<path d="m5 3 4 4-2 3a14 14 0 0 0 7 7l3-2 4 4c-2 6-9 2-13-2S0 5 5 3Z"/>',
  score: '<path d="m4 17 5-6 4 3 7-10M15 4h5v5"/>',
  alert: '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17v.5"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13 7 4 4"/>',
  play: '<path d="M7 4v16l13-8L7 4Z"/>',
};
const icon = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[n] || paths.chat}</svg>`;
const nav = [['overview', 'Обзор', 'overview'], ['conversations', 'Диалоги', 'chat'], ['queue', 'Очередь проверки', 'alert'], ['appeals', 'Апелляции', 'appeals'], ['operators', 'Операторы', 'operators'], ['checklist', 'Чек-лист', 'checklist'], ['loop', 'Цикл обучения', 'refresh'], ['quality', 'Точность', 'score'], ['runs', 'Оценка и сдача', 'play']];
// Extra pages (queue, loop, quality, runs) live in platform.js: window.PAGES, window.pageLoad, window.onPlatformAction.

const TYPES = {
  A: 'A — ошибка оценки: правило ясное, модель ошиблась (ответ меняется)',
  B: 'B — пробел в правилах: случай не описан, решает проверяющий',
  C: 'C — несоразмерный вес: ответ верный, вес спорный (ответ остаётся)',
  D: 'D — апелляция необоснованна (ответ остаётся)',
};
const APPEAL_STATUS = {open: 'Открыта', assigned: 'Назначена', decided: 'Решение принято', closed: 'Закрыта'};
const VERSION_STATUS = {active: 'Активна', draft: 'Черновик', archived: 'Архив', superseded: 'Заменена'};

let state = {
  page: 'overview', conversations: [], overview: {}, people: [], operators: [], appeals: [], checklist: null, versions: [], gross: new Set(),
  search: '', status: 'all', language: 'all', channel: 'all', offset: 0, ready: false,
  card: null, cardEval: null, pickedTurn: null, editChecklist: null, evalRun: null,
};
let loadId = 0, cardId = 0;
const pageSize = 6;
const isCall = (channel) => /call|voice/i.test(channel);
const number = (v) => v == null ? '—' : Number(v).toLocaleString('ru-RU', {maximumFractionDigits: 2});
const refOf = (r) => String(r || '').replace('@v', '@');

// ------------------------------------------------------------------ actor and API
function actor() {
  const v = $('#actor').value;
  return v === 'operator' ? (state.card?.conversation?.operator_id || 'op-01') : v;
}
function role() {
  const a = $('#actor').value;
  return a === 'operator' ? 'operator' : a.startsWith('r-') ? 'reviewer' : a.startsWith('s-') ? 'supervisor' : 'admin';
}
const can = {
  evaluate: () => ['admin', 'supervisor'].includes(role()),
  appeal: () => role() === 'operator',
  decide: () => ['reviewer', 'admin'].includes(role()),
  editChecklist: () => role() === 'admin',
};
async function api(name, params = {}) {
  const response = await fetch(`/api/v1/${name}`, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CQA-Actor': actor()}, body: JSON.stringify(params),
  });
  const body = await response.json().catch(() => ({ok: false, error: {message: response.statusText}}));
  if (!response.ok || !body.ok) throw Error(body.error?.message || 'Не удалось выполнить запрос');
  return body.data;
}
async function act(fn, done) {
  try { await fn(); if (done) toast(done); } catch (e) { toast('Ошибка: ' + e.message, true); }
}

function name(id) { return state.people.find((p) => p.person_id === id)?.name || id || 'Не назначен'; }
const openAppeals = () => state.appeals.filter((a) => ['open', 'assigned'].includes(a.status)).length;

// ------------------------------------------------------------------ layout pieces
function navRender() {
  $('#nav').innerHTML = nav.map(([id, label, i]) => `<a href="#${id}" class="nav-link ${state.page === id ? 'active' : ''}" ${state.page === id ? 'aria-current="page"' : ''}>${icon(i)}<span>${label}</span>${id === 'appeals' && openAppeals() ? `<span class="nav-count">${openAppeals()}</span>` : ''}</a>`).join('');
  $('#crumb').textContent = nav.find((n) => n[0] === state.page)?.[1] || 'Обзор';
}
function header(title, sub, actions = '') { return `<div class="page-heading"><div><h1>${title}</h1><p class="subtitle">${sub}</p></div><div class="actions">${actions}</div></div>`; }
const exportButton = () => `<button class="button" data-action="export">${icon('export')} Экспорт</button>`;
const refreshButton = () => `<button class="button" data-action="refresh">${icon('refresh')} Обновить</button>`;
function stat(title, value, note, i, green = false, link = '') {
  return `<div class="stat ${link ? 'stat-link' : ''}" ${link ? `data-goto="${link}" role="button" tabindex="0"` : ''}><div class="stat-top">${title}${icon(i)}</div><div class="stat-value">${value}</div><div class="stat-note ${green ? 'green' : ''}">${note}</div></div>`;
}

// ------------------------------------------------------------------ pages
function overview() {
  const o = state.overview, total = state.conversations.length;
  const published = state.conversations.filter((c) => c.published_evaluation).length;
  const ratio = total ? Math.round(published / total * 100) : 0;
  return header('Обзор качества', 'Всё, что нужно для внимания к клиенту — в одном месте.', `${refreshButton()}${exportButton()}`) + `
<section class="hero"><div><div class="eyebrow">КАЧЕСТВО НАЧИНАЕТСЯ С ДИАЛОГА</div><h2>Лучше понимаем клиентов.<br>Вместе улучшаем сервис.</h2><p>Объективная оценка, прозрачные решения и внимание к деталям.</p><a class="button primary" href="#conversations">Перейти к диалогам ${icon('arrow')}</a></div><div class="hero-art" aria-hidden="true"><div class="orbit"></div><div class="orbit two"></div><span class="float-dot"></span><svg class="hero-symbol" viewBox="0 0 100 118"><defs><linearGradient id="shield" x1="0" x2="1" y2="1"><stop stop-color="#b2e27e"/><stop offset=".5" stop-color="#7fbc4a"/><stop offset="1" stop-color="#3a8128"/></linearGradient></defs><path d="M6 5h88l-8 62c-3 22-20 37-36 45C31 103 15 88 12 67Z" fill="url(#shield)"/><path d="m31 55 14 15 28-32" stroke="white" stroke-width="8" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity=".92"/></svg><span class="float-tag">✓ &nbsp; Прозрачность в каждой оценке</span></div></section>
<section class="stats" aria-label="Ключевые показатели">${stat('Средний балл', `${number(o.mean_final_total_published)} <small>/ 100</small>`, `По ${number(o.published || 0)} опубликованным оценкам`, 'score', true)}${stat('Оценки опубликованы', `${number(published)} <small>/ ${total}</small>`, 'Доступны для проверки', 'checklist', false, 'evaluated')}${stat('Грубые ошибки', number(state.gross.size), 'Диалоги с выявленным нарушением', 'alert', false, 'gross')}${stat('Открытые апелляции', number(openAppeals()), 'Ожидают решения проверяющего', 'appeals', false, 'appeals')}</section>
<section class="analysis-grid"><div class="panel"><div class="panel-head"><h2>Покрытие оценкой</h2><span class="micro">Набор DEV</span></div><div class="distribution"><div class="donut" style="--fill:${ratio * 3.6}deg"><div class="donut-inner">${ratio}%<span>опубликовано</span></div></div><div class="legend"><div class="legend-row"><span><i class="key"></i>С опубликованной оценкой</span><b>${published}</b></div><div class="legend-row"><span><i class="key light"></i>Без опубликованной оценки</span><b>${total - published}</b></div><div class="legend-row"><span><i class="key amber"></i>Грубые ошибки в оценках</span><b>${number(state.gross.size)}</b></div></div></div></div><div class="panel"><div class="panel-head"><h2>Языки диалогов</h2><span class="micro">${total} диалогов</span></div>${['ru', 'kz', 'en'].map((l, i) => { const n = state.conversations.filter((c) => normalizeLang(c.language) === l).length; return `<div class="language-row"><span class="language-code">${['RU', 'KZ', 'EN'][i]}</span><span class="bar"><i style="width:${total ? n / total * 100 : 0}%"></i></span><b>${n}</b></div>`; }).join('')}</div></section>
<div class="section-title"><h2>Диалоги <span>${total}</span></h2><a class="button text" href="#conversations">Все диалоги ${icon('arrow')}</a></div>${table(true)}`;
}
function normalizeLang(l) { l = String(l).toLowerCase(); return l === 'kk' ? 'kz' : l; }
function filtered() {
  const q = state.search.toLowerCase();
  return state.conversations.filter((c) => (!q || `${c.conversation_id} ${name(c.operator_id)} ${c.operator_id || ''}`.toLowerCase().includes(q)) &&
    (state.status === 'all' || (state.status === 'evaluated' ? c.published_evaluation : state.status === 'appeals' ? c.open_appeals : state.status === 'gross' ? state.gross.has(c.conversation_id) : !c.published_evaluation)) &&
    (state.language === 'all' || normalizeLang(c.language) === state.language) && (state.channel === 'all' || c.channel === state.channel));
}
function table() {
  const rows = filtered(), start = state.offset * pageSize, shown = rows.slice(start, start + pageSize);
  return `<div class="table-panel"><div class="table-toolbar"><div class="tabs" role="group" aria-label="Статус диалога">${[['all', 'Все диалоги'], ['evaluated', 'Оценены'], ['gross', 'Грубые ошибки'], ['appeals', 'С апелляцией']].map(([v, t]) => `<button class="tab ${state.status === v ? 'active' : ''}" data-status="${v}" aria-pressed="${state.status === v}">${t}</button>`).join('')}</div><label class="search">${icon('search')}<input id="search" type="search" placeholder="Найти диалог или оператора" aria-label="Найти диалог или оператора" value="${esc(state.search)}"></label></div><div id="table-content">${tableRows(rows, shown, start)}</div></div>`;
}
function statusPill(c) {
  if (c.open_appeals) return '<span class="pill amber">На апелляции</span>';
  if (state.gross.has(c.conversation_id)) return '<span class="pill red">Грубая ошибка</span>';
  return c.published_evaluation ? '<span class="pill">✓ Оценён</span>' : '<span class="pill neutral">Ожидает оценки</span>';
}
function tableRows(rows, shown, start) {
  return `<div class="table-scroll"><table><thead><tr><th>Диалог</th><th>Оператор</th><th>Канал</th><th>Язык</th><th>Балл</th><th>Статус</th><th></th></tr></thead><tbody>${shown.map((c) => `<tr><td><button class="id-link" data-card="${esc(c.conversation_id)}">${esc(c.conversation_id)}</button></td><td><span class="operator"><span class="avatar">${esc(name(c.operator_id)[0])}</span>${esc(name(c.operator_id))}</span></td><td><span class="channel">${icon(isCall(c.channel) ? 'call' : 'chat')}${isCall(c.channel) ? 'Звонок' : 'Чат'}</span></td><td>${esc(normalizeLang(c.language).toUpperCase())}</td><td><span class="score ${c.final_total != null && c.final_total < 75 ? 'low' : ''}">${number(c.final_total)}</span></td><td>${statusPill(c)}</td><td><button class="icon-button row-arrow" data-card="${esc(c.conversation_id)}" aria-label="Открыть диалог ${esc(c.conversation_id)}">↗</button></td></tr>`).join('')}</tbody></table>${!shown.length ? '<div class="empty">Диалоги не найдены.<br>Попробуйте изменить поиск или фильтры.</div>' : ''}</div><div class="table-footer"><span>${rows.length ? `${start + 1}–${Math.min(start + pageSize, rows.length)} из ${rows.length} диалогов` : '0 диалогов'}</span><div class="pagination"><button data-page="-1" ${!state.offset ? 'disabled' : ''} aria-label="Предыдущая страница">‹</button><span class="page-number">${state.offset + 1}</span><button data-page="1" ${start + pageSize >= rows.length ? 'disabled' : ''} aria-label="Следующая страница">›</button></div></div>`;
}
function conversations() {
  return header('Диалоги', 'Каждый разговор — возможность сделать сервис лучше.', exportButton()) + `<div class="filters"><select id="language" class="filter-select" aria-label="Язык"><option value="all">Все языки</option><option value="ru">Русский</option><option value="kz">Казахский</option><option value="en">Английский</option></select><select id="channel" class="filter-select" aria-label="Канал"><option value="all">Все каналы</option>${[...new Set(state.conversations.map((c) => c.channel))].map((c) => `<option value="${esc(c)}">${isCall(c) ? 'Звонки' : 'Чаты'}</option>`).join('')}</select><button class="button" data-action="reset">Сбросить фильтры</button></div>${table()}`;
}
function operators() {
  return header('Операторы', 'Результаты команды по опубликованным оценкам.', refreshButton()) + `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Оператор</th><th>Оценок</th><th>Средний балл</th><th>Грубые ошибки</th><th>Апелляции</th></tr></thead><tbody>${state.operators.map((o) => `<tr><td><span class="operator"><span class="avatar">${esc((o.name || o.operator_id)[0])}</span>${esc(o.name || o.operator_id)}</span></td><td>${o.evaluations}</td><td class="score">${number(o.mean_total)}</td><td>${o.gross_errors}</td><td>${o.appeals}</td></tr>`).join('')}</tbody></table>${!state.operators.length ? `<div class="empty">${(state.failed || []).some((f) => f.startsWith('analytics.operators')) ? 'Статистика операторов сейчас недоступна — ошибка на сервере.' : 'Операторы ещё не добавлены в справочник.'}</div>` : ''}</div></div><p class="subtitle" style="margin-top:18px">Средний балл рассчитан по опубликованным оценкам с учётом решений проверяющих. Показатели — с размером выборки, без автоматических ярлыков.</p>`;
}
function appeals() {
  return header('Апелляции', 'Прозрачный пересмотр оценок с опорой на доказательства. Решение проверяющего окончательное.', refreshButton()) + `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>Апелляция</th><th>Диалог</th><th>Критерий</th><th>Оператор</th><th>Решение</th><th>Статус</th></tr></thead><tbody>${state.appeals.map((a) => `<tr><td><button class="id-link" data-appeal="${esc(a.appeal_id)}">${esc(a.appeal_id)}</button></td><td><button class="id-link" data-card="${esc(a.conversation_id)}">${esc(a.conversation_id)}</button></td><td>${esc(a.criterion_id)}</td><td>${esc(name(a.operator_id))}</td><td>${a.decision ? `${esc(a.decision.action === 'change' ? 'Изменено' : 'Подтверждено')} · тип ${esc(a.decision.type)}` : '—'}</td><td><span class="pill ${['open', 'assigned'].includes(a.status) ? 'amber' : ''}">${esc(APPEAL_STATUS[a.status] || a.status)}</span></td></tr>`).join('')}</tbody></table>${!state.appeals.length ? '<div class="empty">Пока нет апелляций.<br>Оператор подаёт апелляцию из карточки оценки диалога.</div>' : ''}</div></div>`;
}

// ------------------------------------------------------------------ checklist page
function checklist() {
  const ck = state.checklist, active = state.versions.find((v) => v.status === 'active');
  const actions = `<span class="pill">Версия ${esc(ck?.meta?.version || '—')}</span>` +
    (can.editChecklist() ? `<button class="button primary" data-action="edit-checklist">${icon('edit')} Изменить чек-лист</button>` : '');
  const versions = `<div class="panel version-panel"><div class="panel-head"><h2>Версии чек-листа</h2><span class="micro">Новая версия — черновик, пока администратор её не активирует</span></div>${state.versions.map((v) => `<div class="version-row"><b>${esc(v.ref)}</b><span class="pill ${v.status === 'active' ? '' : v.status === 'draft' ? 'amber' : 'neutral'}">${esc(VERSION_STATUS[v.status] || v.status)}</span><span class="version-meta">${esc((v.created_at || '').slice(0, 16).replace('T', ' '))} · ${esc(v.author)} · критериев ${esc(v.criteria)}<br>${esc(v.reason || '')}</span><span class="version-actions"><button class="button" data-view-version="${esc(v.ref)}">Открыть</button>${can.editChecklist() && v.status !== 'active' ? `<button class="button primary" data-activate="${esc(v.ref)}">Активировать и пересчитать</button>` : ''}</span></div>`).join('')}</div>`;
  if (state.editChecklist) return header('Изменение чек-листа', `Правка ${esc(state.editChecklist.meta?.checklist_id || 'default')} v${esc(state.editChecklist.meta?.version)}. Сохранение создаёт новую неизменяемую версию; удалённые критерии уходят в архив.`, `<button class="button" data-action="cancel-edit">Отмена</button><button class="button" data-action="add-criterion">Добавить критерий</button><button class="button primary" data-action="save-checklist">Сохранить новую версию</button>`) + editor();
  return header('Чек-лист качества', `Единые критерии для последовательной и понятной оценки. Активная версия: ${esc(active?.ref || '—')}.`, actions) + versions +
    `<div class="check-grid">${(ck?.criteria || []).filter((c) => c.status !== 'archived').map((c, i) => `<article class="check-card"><div class="check-card-top"><span>${String(i + 1).padStart(2, '0')} / ${esc(c.id)}</span><span class="pill neutral">${c.type === 'binary' ? 'Да / Нет' : c.type === 'scale' ? 'Шкала баллов' : c.type === 'flag' ? 'Нарушения' : esc(c.type)}</span></div><h3>${esc(c.title)}</h3><p>${esc(c.description || '')}</p>${c.type === 'scale' ? `<ul class="scale-list">${(c.scale || []).map((s) => `<li><b>${esc(s.value)}</b> ${esc(s.text)}</li>`).join('')}</ul>` : ''}${c.na_allowed ? `<p class="na-note">N/A: ${esc(c.na_condition || '')}</p>` : ''}<div class="check-card-bottom">${c.type === 'flag' ? 'Контроль грубых нарушений, без баллов' : `Вес: ${esc(c.weight)} баллов`}${c.na_allowed ? ' &nbsp; · &nbsp; Допускается «Не применимо»' : ''}</div></article>`).join('')}</div>` +
    ((ck?.criteria || []).some((c) => c.status === 'archived') ? `<p class="subtitle" style="margin-top:18px">В архиве: ${(ck.criteria || []).filter((c) => c.status === 'archived').map((c) => esc(c.id + ' ' + c.title)).join(', ')}</p>` : '');
}
function editor() {
  const d = state.editChecklist;
  return `<div class="panel"><label class="field wide">Общие правила (по одному в строке)<textarea id="global-rules" rows="5">${esc((d.global_rules || []).join('\n'))}</textarea></label></div>` +
    `<div class="edit-list">${d.criteria.map((c, i) => `<div class="edit-card ${c.status === 'archived' ? 'archived' : ''}" data-index="${i}"><div class="edit-head"><span class="pill neutral">${esc(c.id)}</span>${c.status === 'archived' ? '<span class="pill neutral">в архиве</span>' : ''}<button class="button" data-toggle-archive="${i}">${c.status === 'archived' ? 'Вернуть из архива' : 'В архив'}</button></div><div class="form-grid">
      <label class="field wide">Название<input class="f-title" value="${esc(c.title)}"></label>
      <label class="field wide">Формулировка (когда «Да» / как оценивать)<textarea class="f-description" rows="2">${esc(c.description || '')}</textarea></label>
      <label class="field">Тип<select class="f-type"><option value="binary" ${c.type === 'binary' ? 'selected' : ''}>Да / Нет</option><option value="scale" ${c.type === 'scale' ? 'selected' : ''}>Шкала</option><option value="flag" ${c.type === 'flag' ? 'selected' : ''}>Флаг нарушения</option></select></label>
      <label class="field">Вес<input class="f-weight" type="number" min="0" value="${esc(c.weight)}"></label>
      <label class="field">Раздел<select class="f-section">${d.sections.map((s) => `<option value="${esc(s.id)}" ${c.section === s.id ? 'selected' : ''}>${esc(s.title)}</option>`).join('')}</select></label>
      <label class="field check"><input class="f-na" type="checkbox" ${c.na_allowed ? 'checked' : ''}> Допускается N/A</label>
      <label class="field wide">Условие N/A<input class="f-na-condition" value="${esc(c.na_condition || '')}"></label>
      <label class="field wide">Ступени шкалы (значение:описание | …)<input class="f-scale" value="${esc((c.scale || []).map((s) => s.value + ':' + s.text).join(' | '))}" placeholder="10:Ответы ясные | 7:Один сбой | 0:Ход диалога непонятен"></label>
    </div></div>`).join('')}</div>`;
}
function readEditor() {
  const d = state.editChecklist;
  d.global_rules = $('#global-rules').value.split('\n').map((l) => l.trim()).filter(Boolean);
  $$('.edit-card').forEach((el) => {
    const c = d.criteria[Number(el.dataset.index)];
    const type = el.querySelector('.f-type').value;
    Object.assign(c, {title: el.querySelector('.f-title').value.trim(), description: el.querySelector('.f-description').value.trim(), type,
      weight: Number(el.querySelector('.f-weight').value), section: el.querySelector('.f-section').value,
      na_allowed: el.querySelector('.f-na').checked, na_condition: el.querySelector('.f-na-condition').value.trim() || null});
    if (type === 'scale') {
      c.scale = el.querySelector('.f-scale').value.split('|').map((p) => p.trim()).filter(Boolean).map((p) => { const i = p.indexOf(':'); return {value: Number(p.slice(0, i)), text: p.slice(i + 1).trim()}; });
    } else delete c.scale;
  });
}

// ------------------------------------------------------------------ render and load
function render() {
  navRender();
  if (!state.ready) return;
  const pages = {overview, conversations, operators, appeals, checklist, ...(window.PAGES || {})};
  $('#page').innerHTML = (pages[state.page] || overview)();
  if ($('#language')) $('#language').value = state.language;
  if ($('#channel')) $('#channel').value = state.channel;
}
function tableUpdate() {
  const rows = filtered(), start = state.offset * pageSize;
  $('#table-content').innerHTML = tableRows(rows, rows.slice(start, start + pageSize), start);
  $$('[data-status]').forEach((b) => { b.classList.toggle('active', b.dataset.status === state.status); b.setAttribute('aria-pressed', String(b.dataset.status === state.status)); });
}
async function load() {
  const id = ++loadId;
  try {
    // Sequential on purpose: analytics commands rebuild the SQLite projection, parallel calls race on it.
    const failed = [];
    const optional = async (n, fallback) => { try { return await api(n); } catch (err) { failed.push(`${n}: ${err.message}`); return fallback; } };
    const conversations = await api('dataset.list'), appeals = await api('appeal.list'), versions = await api('checklist.list');
    const overview = await optional('analytics.overview', {}), people = await optional('people.list', []);
    const operators = await optional('analytics.operators', []), gross = await optional('jury.gross_errors', {items: []});
    state.failed = failed;
    if (failed.length) toast('Часть данных недоступна: ' + failed.join('; '), true);
    const active = versions.find((v) => v.status === 'active') || versions[versions.length - 1];
    const ck = await api('checklist.get', active ? {ref: refOf(active.ref)} : {});
    if (id !== loadId) return;
    Object.assign(state, {conversations, overview, people, operators, appeals, versions, checklist: ck, gross: new Set((gross.items || []).map((g) => g.conversation_id)), ready: true});
    render();
  } catch (e) {
    if (id !== loadId) return;
    $('#page').innerHTML = header('Качество сервиса', 'Рабочее пространство Freedom') + `<div class="error"><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p><p>Проверьте, что локальный сервер платформы запущен.</p><button class="button" data-action="refresh">Попробовать снова</button></div>`;
  }
}
function route() {
  const next = location.hash.slice(1) || 'overview';
  state.page = nav.some((n) => n[0] === next) ? next : 'overview';
  Object.assign(state, {offset: 0, search: '', status: state.pendingStatus || 'all', language: 'all', channel: 'all', editChecklist: null, pendingStatus: null});
  $('#sidebar').classList.remove('open');
  $('#menu-toggle').setAttribute('aria-expanded', 'false');
  render();
  if (window.pageLoad) window.pageLoad(state.page);
}

// ------------------------------------------------------------------ dialog card
function showDialog(title, sub, content) {
  $('#dialog-title').innerHTML = `<h2>${esc(title)}</h2><p>${esc(sub)}</p>`;
  $('#dialog-body').innerHTML = content;
  if (!$('#detail').open) $('#detail').showModal();
}
function answerLabel(c, a) {
  if (a === null || a === undefined) return 'Нет ответа';
  if (a === 'na') return 'Н/П';
  if (c.type === 'scale') return `${a} из ${Math.max(0, ...(c.scale || state.checklist?.criteria?.find((x) => x.id === c.criterion_id)?.scale || []).map((s) => s.value))}`;
  if (c.type === 'flag') return a === 'yes' ? 'Нарушение' : 'Нарушений нет';
  return a === 'yes' ? 'Да' : 'Нет';
}
function answerPill(c, a) {
  const bad = (c.type === 'flag' && a === 'yes') || (c.type !== 'flag' && a === 'no') || (c.type === 'scale' && typeof a === 'number' && a === 0);
  return `<span class="pill ${a === 'na' || a == null ? 'neutral' : bad ? 'red' : c.type === 'scale' ? 'amber' : ''}">${esc(answerLabel(c, a))}</span>`;
}
function answerOptions(c, current) {
  const scale = c.scale || state.checklist?.criteria?.find((x) => x.id === c.criterion_id)?.scale || [];
  const opts = c.type === 'scale' ? scale.map((s) => [s.value, `${s.value} — ${s.text}`]) : c.type === 'flag' ? [['yes', 'Нарушение есть'], ['no', 'Нарушений нет']] : [['yes', 'Да'], ['no', 'Нет']];
  if (c.na_allowed) opts.push(['na', 'Н/П (не применимо)']);
  return opts.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(current) ? 'selected' : ''}>${esc(t)}</option>`).join('');
}
function criterionHtml(c, ev, appealsOf, focus) {
  const o = c.original || {}, f = c.final || {}, d = c.decision;
  const checks = Object.fromEntries((o.evidence_checks || []).map((e) => [e.idx, e]));
  const open = appealsOf.find((a) => ['open', 'assigned'].includes(a.status));
  let h = `<details class="criterion" data-criterion="${esc(c.criterion_id)}" ${focus ? 'open' : ''}><summary>${esc(c.criterion_id)} · ${esc(c.title)} ${answerPill(c, f.answer)}${f.effective_points != null ? `<span class="points">${number(f.effective_points)} из ${number(f.effective_weight)}</span>` : ''}${open ? '<span class="pill amber">апелляция</span>' : ''}</summary>`;
  if (!d && String(c.origin || '').includes('reviewer')) h += `<div class="decision-note">Ответ перенесён из решения проверяющего в прошлой версии чек-листа (пересчёт без LLM).</div>`;
  if (d) h += `<div class="decision-note">Решение проверяющего ${esc(d.reviewer_id)} · тип ${esc(d.type)}: ${esc(answerLabel(c, o.answer))} → <b>${esc(answerLabel(c, d.action === 'change' ? d.new_answer : o.answer))}</b>. ${esc(d.reason)}</div>`;
  h += `<p>${esc(o.explanation || 'Объяснение отсутствует')}</p>`;
  h += (o.evidence || []).map((e) => { const k = checks[e.idx]; return `<p class="evidence"><button class="turn-link" data-jump="${esc(e.idx)}">#${esc(e.idx)}</button> «${esc(e.quote)}» ${k ? (k.exists && k.quote_ok ? '<span class="ok">✓ цитата совпадает</span>' : '<span class="bad">✗ цитата не найдена в диалоге</span>') : ''}</p>`; }).join('');
  const meta = [];
  if (o.confidence != null) meta.push(`уверенность модели ${o.confidence}`);
  if (o.checked_range) meta.push(`проверен участок #${o.checked_range.join('–#')}`);
  if (o.insufficient_data) meta.push('<span class="bad">модель отметила нехватку данных</span>');
  if (meta.length) h += `<p class="meta">${meta.join(' · ')}</p>`;
  if ((o.ambiguities || []).length) h += `<p class="meta">Неоднозначности: ${o.ambiguities.map(esc).join('; ')}</p>`;
  if (o.reasoning) h += `<details class="sub"><summary>Рассуждение модели</summary><p>${esc(o.reasoning)}</p></details>`;
  appealsOf.forEach((a) => {
    h += `<div class="appeal-note"><b>Апелляция ${esc(a.appeal_id)} · ${esc(APPEAL_STATUS[a.status] || a.status)}</b> · ${esc(name(a.operator_id))}${a.turn_idx ? ` · реплика <button class="turn-link" data-jump="${esc(a.turn_idx)}">#${esc(a.turn_idx)}</button>` : ''}<br>«${esc(a.comment)}»</div>`;
  });
  if (can.appeal() && !open) {
    h += `<div class="action-box"><b>Оспорить оценку</b><label class="field wide">Почему вы не согласны<textarea class="appeal-comment" rows="3" placeholder="Что именно оспаривается и почему"></textarea></label><label class="field">Реплика — кликните в расшифровке<input class="turn-input" type="number" min="1" value="${esc(state.pickedTurn ?? '')}"></label><button class="button primary" data-file-appeal="${esc(c.criterion_id)}">Отправить апелляцию</button></div>`;
  }
  if (can.decide() && open) {
    h += `<div class="action-box review" data-appeal-id="${esc(open.appeal_id)}"><b>Решение проверяющего — окончательное</b><div class="form-grid"><label class="field">Действие<select class="dec-action"><option value="confirm">Подтвердить оценку</option><option value="change">Изменить ответ</option></select></label><label class="field">Новый ответ<select class="dec-answer">${answerOptions(c, f.answer)}</select></label><label class="field wide">Тип апелляции<select class="dec-type">${Object.entries(TYPES).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join('')}</select></label><label class="field wide">Причина решения<textarea class="dec-reason" rows="3" placeholder="Попадёт в историю и в цикл обучения"></textarea></label></div><button class="button primary" data-decide="${esc(c.criterion_id)}">Сохранить решение</button><span class="meta"> Исходная оценка не меняется — итог пересчитывается поверх неё.</span></div>`;
  }
  if (can.decide() && d?.decision_id) h += `<button class="button" data-precedent="${esc(d.decision_id)}">Сделать прецедентом для промпта</button>`;
  return h + '</details>';
}
function cardHtml(data, focusCriterion) {
  const c = data.conversation, ev = data.evaluation;
  const transcript = `<section><h2>Расшифровка диалога</h2><p class="meta">Клик по реплике — ссылка для апелляции.</p>${c.transcript.map((t) => `<div class="message ${/operator|agent|оператор/i.test(t.speaker) ? 'agent' : ''}" id="turn-${t.idx}" data-turn="${t.idx}"><small>${esc(t.speaker)} ${esc(t.timestamp || '')} · #${t.idx}</small>${esc(t.text)}</div>`).join('') || '<div class="empty">Нет расшифровки</div>'}${c.audio_url ? `<p><a class="button" href="${esc(c.audio_url)}" target="_blank" rel="noopener">Запись звонка ↗</a></p>` : ''}</section>`;
  const tools = `<div class="card-tools">${(data.evaluations || []).length > 1 ? `<select id="eval-select" class="filter-select" aria-label="Оценки диалога">${data.evaluations.map((x) => `<option value="${esc(x.evaluation_id)}" ${x.evaluation_id === ev?.evaluation_id ? 'selected' : ''}>${esc(x.agent)} · ${esc(x.checklist)} · ${number(x.total)}${x.published ? ' · опубл.' : ''}</option>`).join('')}</select>` : ''}${ev ? `<button class="button" data-action="trace">Откуда ответ</button>` : ''}${can.evaluate() ? `<button class="button primary" data-action="evaluate">${icon('play')} Оценить диалог</button>` : ''}</div><div id="evaluate-box"></div><div id="trace-box"></div>`;
  let right = `<section><h2>Оценка качества</h2>${tools}`;
  if (!ev) {
    right += `<div class="empty">Оценка ещё не выполнена.<br>${can.evaluate() ? 'Нажмите «Оценить диалог»: система соберёт промпт по активному чек-листу.' : 'Её запускает администратор или руководитель.'}</div></section>`;
    return `<div class="detail-grid">${transcript}${right}</div>`;
  }
  const appealsOf = (cid) => state.appeals.filter((a) => a.evaluation_id === ev.evaluation_id && a.criterion_id === cid);
  right += `<div class="score-head"><p class="stat-value">${number(ev.final_total)} <small>/ 100</small></p>${ev.gross_error ? '<span class="pill red">Грубая ошибка</span>' : '<span class="pill">Грубых ошибок нет</span>'}</div>` +
    (ev.original_total !== ev.final_total ? `<p class="meta">Оценка модели: ${number(ev.original_total)} → после решений проверяющих: <b>${number(ev.final_total)}</b></p>` : '') +
    `<p class="subtitle">Модель: ${esc(ev.agent)} · ${esc(ev.checklist)} · шаблон ${esc(ev.versions?.template?.name || '—')}. Баллы считает программа: веса, перераспределение Н/П, флаг грубой ошибки отдельно.</p>` +
    `<div class="sections">${(ev.sections || []).map((s) => `<div><span>${esc(s.title)}</span><b>${s.score_pct == null ? '—' : number(s.score_pct) + '%'}</b></div>`).join('')}</div>` +
    ((ev.issues || []).length ? `<div class="error">Замечания проверки ответа: ${ev.issues.map((i) => esc(typeof i === 'string' ? i : JSON.stringify(i))).join('; ')}</div>` : '') +
    (ev.dialog_facts?.dialog_summary ? `<p class="summary">${esc(ev.dialog_facts.dialog_summary)}</p>` : '') +
    (ev.criteria || []).map((cr) => criterionHtml(cr, ev, appealsOf(cr.criterion_id), cr.criterion_id === focusCriterion)).join('') + '</section>';
  return `<div class="detail-grid">${transcript}${right}</div>`;
}
async function openCard(id, evaluationId, focusCriterion) {
  const request = ++cardId;
  if (!$('#detail').open) showDialog(id, 'Диалог и оценка', '<div class="empty">Загружаем диалог…</div>');
  try {
    const params = {conversation_id: id};
    if (evaluationId) params.evaluation_id = evaluationId;
    const data = await api('conversation.card', params);
    if (request !== cardId || !$('#detail').open) return;
    state.card = data; state.cardEval = data.evaluation?.evaluation_id || null; state.pickedTurn = null;
    const c = data.conversation;
    showDialog(id, `${isCall(c.channel) ? 'Звонок' : 'Чат'} · ${normalizeLang(c.language).toUpperCase()} · ${name(c.operator_id)}${c.operator_id ? ' (' + c.operator_id + ')' : ''}`, cardHtml(data, focusCriterion));
    if (focusCriterion) $(`[data-criterion="${focusCriterion}"]`)?.scrollIntoView({block: 'start'});
  } catch (e) {
    if (request === cardId && $('#detail').open) $('#dialog-body').innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}
async function refreshCard(done) {
  await load();
  if (state.card) await openCard(state.card.conversation.conversation_id, state.cardEval);
  if (done) toast(done);
}
async function openAppeal(id) {
  const a = state.appeals.find((x) => x.appeal_id === id);
  if (!a) return;
  await openCard(a.conversation_id, a.evaluation_id, a.criterion_id);
}

// ------------------------------------------------------------------ trace and manual evaluation
async function showTrace() {
  const t = await api('trace', {conversation_id: state.card.conversation.conversation_id, evaluation_id: state.cardEval, include_text: true});
  const v = t.versions || {};
  $('#trace-box').innerHTML = `<div class="action-box"><b>Откуда этот ответ</b><p class="meta">Чек-лист ${esc(v.checklist?.ref)} (хеш ${esc(v.checklist?.hash)}) · шаблон ${esc(v.template?.name)} · параметры v${esc(v.params?.version)} · уточнений правил: ${(v.rule_clarifications || []).length} · прецедентов: ${(v.precedents || []).length}</p><p class="meta">Цепочка оценок: ${(t.lineage || []).map(esc).join(' ← ')}</p>${(t.prompts || []).map((p) => `<p class="meta">Промпт: ${esc(p.prompt_file || 'недоступен (пересчёт без LLM)')} · sha ${esc(p.prompt_sha || '—')}</p>${p.prompt_text ? `<details class="sub"><summary>Текст промпта</summary><pre>${esc(p.prompt_text)}</pre></details>` : ''}${p.response_text ? `<details class="sub"><summary>Сырой ответ модели</summary><pre>${esc(p.response_text)}</pre></details>` : ''}`).join('')}<details class="sub"><summary>Действия людей (${(t.human_actions || []).length})</summary>${(t.human_actions || []).map((h) => `<p class="meta">${esc((h.ts || '').slice(0, 16).replace('T', ' '))} · ${esc(h.type)} · ${esc(h.actor)}${h.reason ? ' · ' + esc(h.reason) : ''}</p>`).join('')}</details></div>`;
}
async function showEvaluate() {
  const list = await api('providers.list').catch(() => ({providers: []}));
  const agents = (list.providers || []).filter((p) => p.type === 'command' && p.enabled && p.available);
  state.providers = agents;
  $('#evaluate-box').innerHTML = `<div class="action-box"><b>Оценить диалог</b><p class="meta">Промпт собирается по активной версии чек-листа. Кто оценивает:</p>
<div class="provider-list">${agents.map((p, i) => `<label class="provider-option"><input type="radio" name="eval-provider" value="${esc(p.name)}" ${i === 0 ? 'checked' : ''}><span><b>${esc(p.label)}</b><small>${p.detach ? 'Откроется новый чат в окне IDE; ответ вставьте сюда' : 'Новая сессия без контекста: пустая папка, без инструментов и истории. Ответ вернётся сам'}</small></span></label>`).join('')}
<label class="provider-option"><input type="radio" name="eval-provider" value="manual" ${agents.length ? '' : 'checked'}><span><b>Вручную</b><small>Скопировать промпт в любой чат модели и вставить ответ</small></span></label></div>
<label class="field">Шаблон промпта<input id="eval-template" value="v2"></label><button class="button primary" data-action="prepare-eval">${icon('play')} Запустить оценку</button><div id="eval-step2"></div></div>`;
}
async function prepareEval() {
  const id = state.card.conversation.conversation_id;
  const provider = document.querySelector('input[name="eval-provider"]:checked')?.value || 'manual';
  const spec = (state.providers || []).find((p) => p.name === provider);
  const agent = provider === 'manual' ? 'manual' : provider;
  const runId = `ui-${id}-${new Date().toISOString().replace(/\D/g, '').slice(0, 14)}`;
  await api('run.prepare', {run_id: runId, ids: [id], agents: [agent], template: $('#eval-template').value.trim() || 'v2'});
  state.evalRun = {runId, agent};
  const next = await api('paste.next', {run: runId, agent});
  const promptBlock = `<details class="sub"><summary>Текст промпта</summary><pre id="prompt-text">${esc(next.text)}</pre></details>`;
  const pasteBlock = `<label class="field wide">Ответ модели (JSON целиком)<textarea id="answer-text" rows="7" placeholder="Вставьте ответ модели"></textarea></label><button class="button primary" data-action="submit-eval">Проверить, оценить и опубликовать</button><div id="eval-result"></div>`;
  if (provider === 'manual') {
    $('#eval-step2').innerHTML = `<p class="meta">Прогон ${esc(runId)} · промпт готов</p><button class="button" data-action="copy-prompt">Скопировать промпт</button>${promptBlock}${pasteBlock}`;
    return;
  }
  if (spec?.detach) {
    await api('run.dispatch', {run_id: runId, agent, provider});
    $('#eval-step2').innerHTML = `<p class="meta">Прогон ${esc(runId)} · в ${esc(spec.label)} открыт новый чат с промптом. Когда модель ответит, скопируйте ответ сюда.</p>${promptBlock}${pasteBlock}`;
    return;
  }
  const started = Date.now();
  $('#eval-step2').innerHTML = `<p class="meta running">${esc(spec?.label || provider)} оценивает диалог в новой сессии без контекста… обычно 1–3 минуты. <span id="eval-timer">0 с</span></p>${promptBlock}`;
  const timer = setInterval(() => { const t = $('#eval-timer'); if (t) t.textContent = Math.round((Date.now() - started) / 1000) + ' с'; }, 1000);
  try {
    const res = await api('run.dispatch', {run_id: runId, agent, provider});
    const d = (res.details || [])[0] || {};
    if (d.status !== 'done') {
      $('#eval-step2').innerHTML = `<div class="error">Агент не вернул ответ (${esc(d.status || 'нет данных')}).<pre>${esc(d.error || d.stderr || '')}</pre>Можно оценить вручную: скопируйте промпт и вставьте ответ.</div><button class="button" data-action="copy-prompt">Скопировать промпт</button>${promptBlock}${pasteBlock}`;
      throw Error('агент не вернул ответ');
    }
    const ing = await api('run.ingest', {run_id: runId, agents: [agent]});
    if (!ing[agent]?.ok && !ing[agent]?.warn) {
      $('#eval-step2').innerHTML = `<div class="error">Ответ агента не прошёл проверку. Его можно посмотреть в прогоне ${esc(runId)}; оцените заново или вручную.</div>${promptBlock}`;
      throw Error('ответ агента не прошёл проверку');
    }
    const imported = await api('eval.import', {source: `${runId}:${agent}`});
    await api('eval.publish', {evaluation_ids: imported.evaluation_ids});
    await refreshCard(`Оценка от ${spec?.label || provider} за ${Math.round((Date.now() - started) / 1000)} с сохранена и опубликована`);
  } finally { clearInterval(timer); }
}
async function submitEval() {
  const {runId, agent} = state.evalRun;
  const text = $('#answer-text').value.trim();
  if (!text) throw Error('Вставьте ответ модели');
  const saved = await api('paste.save', {run: runId, agent, text});
  if (saved.ok === false || !saved.saved) {
    $('#eval-result').innerHTML = `<div class="error">Ответ не принят: ${esc((saved.errors || ['не удалось разобрать JSON']).join('; '))}. Попросите модель исправить ответ и вставьте его заново.</div>`;
    throw Error('ответ модели не прошёл проверку');
  }
  await api('run.ingest', {run_id: runId, agents: [agent]});
  const imported = await api('eval.import', {source: `${runId}:${agent}`});
  await api('eval.publish', {evaluation_ids: imported.evaluation_ids});
  await refreshCard('Оценка сохранена и опубликована');
}

// ------------------------------------------------------------------ export and toast
function exportCsv() {
  const rows = filtered();
  const cell = (x) => '"' + String(x ?? '').replace(/"/g, '""').replace(/^[=+@-]/, "'$&") + '"';
  const lines = [['Диалог', 'Оператор', 'Канал', 'Язык', 'Балл', 'Грубая ошибка', 'Открытые апелляции'], ...rows.map((c) => [c.conversation_id, name(c.operator_id), c.channel, c.language, c.final_total, state.gross.has(c.conversation_id) ? 'да' : '', c.open_appeals])];
  const blob = new Blob(['﻿' + lines.map((r) => r.map(cell).join(';')).join('\r\n')], {type: 'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = 'freedom-quality-dialogues.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Экспортировано диалогов: ${rows.length}`);
}
let toastTimer;
function toast(message, error = false) {
  $('#toast').textContent = message;
  $('#toast').classList.toggle('error-toast', error);
  $('#toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('show'), error ? 7000 : 3500);
}

// ------------------------------------------------------------------ events
document.addEventListener('click', (e) => {
  const turn = e.target.closest('[data-turn]');
  if (turn) {
    $$('.message.picked').forEach((m) => m.classList.remove('picked'));
    turn.classList.add('picked');
    state.pickedTurn = Number(turn.dataset.turn);
    $$('.turn-input').forEach((i) => { i.value = state.pickedTurn; });
  }
  const goto = e.target.closest('[data-goto]');
  if (goto) { state.pendingStatus = goto.dataset.goto; if (location.hash === '#conversations') route(); else location.hash = 'conversations'; return; }
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.jump) { const m = $(`#turn-${b.dataset.jump}`); if (m) { m.scrollIntoView({behavior: 'smooth', block: 'center'}); m.classList.add('picked'); } return; }
  if (b.dataset.card) { openCard(b.dataset.card); return; }
  if (b.dataset.appeal) { openAppeal(b.dataset.appeal); return; }
  if (b.dataset.status) { state.status = b.dataset.status; state.offset = 0; tableUpdate(); }
  if (b.dataset.page) { state.offset = Math.max(0, state.offset + Number(b.dataset.page)); tableUpdate(); }
  if (b.dataset.fileAppeal) {
    const box = b.closest('.action-box');
    act(async () => {
      const comment = box.querySelector('.appeal-comment').value.trim();
      if (!comment) throw Error('напишите, почему вы не согласны');
      const turnIdx = box.querySelector('.turn-input').value;
      await api('appeal.file', {evaluation_id: state.cardEval, criterion_id: b.dataset.fileAppeal, comment, ...(turnIdx ? {turn_idx: Number(turnIdx)} : {})});
      await refreshCard('Апелляция отправлена проверяющему');
    });
  }
  if (b.dataset.decide) {
    const box = b.closest('.action-box');
    act(async () => {
      const action = box.querySelector('.dec-action').value, reason = box.querySelector('.dec-reason').value.trim(), raw = box.querySelector('.dec-answer').value;
      if (!reason) throw Error('укажите причину решения');
      const params = {appeal_id: box.dataset.appealId, action, reason, type: box.querySelector('.dec-type').value};
      if (action === 'change') params.new_answer = /^-?\d+$/.test(raw) ? Number(raw) : raw;
      await api('appeal.decide', params);
      await refreshCard('Решение сохранено, итог пересчитан');
    });
  }
  if (b.dataset.precedent) act(() => api('precedent.mark', {decision_id: b.dataset.precedent}), 'Решение отмечено как прецедент — оно попадёт в промпт похожих диалогов');
  if (b.dataset.viewVersion) act(async () => { state.checklist = await api('checklist.get', {ref: refOf(b.dataset.viewVersion)}); render(); });
  if (b.dataset.activate) {
    act(async () => {
      const ref = refOf(b.dataset.activate);
      const reason = prompt(`Причина активации ${b.dataset.activate} (попадёт в историю):`);
      if (!reason) return;
      try { await api('version.activate', {ref, reason}); }
      catch (err) {
        if (!confirm(`${err.message}\n\nАктивировать без регрессионной проверки? Причина будет записана в историю.`)) return;
        await api('version.activate', {ref, reason, force: true});
      }
      const res = await api('eval.rescore', {ref, publish: true});
      await load();
      toast(`Активирована ${b.dataset.activate}. Пересчитано без LLM: ${res.rescored}.` + (res.needs_llm?.conversations ? ` Новым критериям нужна оценка моделью: ${res.needs_llm.conversations} диалогов.` : ''));
    });
  }
  if (window.onPlatformAction && window.onPlatformAction(b)) return;
  const a = b.dataset.action;
  if (a === 'export') exportCsv();
  if (a === 'refresh') { toast('Обновляем данные…'); load(); }
  if (a === 'reset') { Object.assign(state, {search: '', status: 'all', language: 'all', channel: 'all', offset: 0}); render(); }
  if (a === 'trace') act(showTrace);
  if (a === 'evaluate') act(showEvaluate);
  if (a === 'prepare-eval') act(prepareEval);
  if (a === 'copy-prompt') act(async () => { await navigator.clipboard.writeText($('#prompt-text').textContent); }, 'Промпт скопирован — вставьте его в новый чат модели');
  if (a === 'submit-eval') act(submitEval);
  if (a === 'edit-checklist') { state.editChecklist = structuredClone(state.checklist); render(); }
  if (a === 'cancel-edit') { state.editChecklist = null; render(); }
  if (a === 'add-criterion') {
    readEditor();
    const d = state.editChecklist;
    let n = d.criteria.length + 1;
    while (d.criteria.some((c) => c.id === 'c' + String(n).padStart(2, '0'))) n++;
    d.criteria.push({id: 'c' + String(n).padStart(2, '0'), section: d.sections[0].id, title: 'Новый критерий', description: '', type: 'binary', weight: 10, na_allowed: false, na_condition: null});
    render();
    $$('.edit-card').pop()?.scrollIntoView({behavior: 'smooth'});
  }
  if (b.dataset.toggleArchive) {
    readEditor();
    const c = state.editChecklist.criteria[Number(b.dataset.toggleArchive)];
    if (c.status === 'archived') delete c.status; else c.status = 'archived';
    render();
  }
  if (a === 'save-checklist') {
    act(async () => {
      readEditor();
      const reason = prompt('Причина изменения чек-листа (попадёт в историю версии):');
      if (!reason) return;
      const doc = state.editChecklist;
      const res = await api('checklist.save', {document: doc, checklist_id: doc.meta?.checklist_id || 'default', reason});
      state.editChecklist = null;
      await load();
      state.checklist = await api('checklist.get', {ref: refOf(res.ref)});
      render();
      toast(`Сохранена версия ${res.ref} (черновик). Чтобы она начала действовать — «Активировать и пересчитать».`);
    });
  }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.closest?.('[data-goto]')) e.target.click(); });
document.addEventListener('input', (e) => { if (e.target.id === 'search') { state.search = e.target.value; state.offset = 0; tableUpdate(); } });
document.addEventListener('change', (e) => {
  if (['language', 'channel'].includes(e.target.id)) { state[e.target.id] = e.target.value; state.offset = 0; tableUpdate(); }
  if (e.target.id === 'eval-select') openCard(state.card.conversation.conversation_id, e.target.value);
});

$('#close-dialog').onclick = () => { $('#detail').close(); cardId++; };
$('#detail').addEventListener('click', (e) => {
  if (e.target === $('#detail')) {
    const r = $('#detail').getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) { $('#detail').close(); cardId++; }
  }
});
$('#menu-toggle').setAttribute('aria-expanded', 'false');
$('#menu-toggle').onclick = () => { const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); };

function updateRole() {
  const label = $('#actor').selectedOptions[0].textContent.split(' · ')[0];
  $('#role-avatar').textContent = label[0];
  $('#profile').textContent = label[0];
  try { localStorage.setItem('cqa.actor', $('#actor').value); } catch {}
}
$('#actor').onchange = () => {
  updateRole();
  state.editChecklist = null;
  render();
  if ($('#detail').open && state.card) openCard(state.card.conversation.conversation_id, state.cardEval);
  toast(`Роль: ${$('#actor').selectedOptions[0].textContent}`);
};
try { const saved = localStorage.getItem('cqa.actor'); if (saved && [...$('#actor').options].some((o) => o.value === saved)) $('#actor').value = saved; } catch {}
updateRole();

function help() {
  showDialog('Качество клиентского сервиса', 'Freedom · Локальная платформа', `<p>Рабочее пространство для анализа диалогов контакт-центра. Роль выбирается внизу меню — права проверяет сервер.</p>
<details class="criterion" open><summary>Диалоги и оценки</summary><p>Откройте диалог: расшифровка, ответ по каждому критерию, объяснение модели и цитаты-доказательства (клик по номеру — переход к реплике). Баллы считает программа, грубая ошибка — отдельный флаг. «Откуда ответ» показывает промпт, сырой ответ модели и версии.</p></details>
<details class="criterion"><summary>Апелляции</summary><p>Оператор оспаривает критерий в карточке диалога. Проверяющий подтверждает или меняет ответ, указывает причину и тип A–D — его решение окончательное; исходная оценка сохраняется, итог пересчитывается поверх. Решение можно сделать прецедентом для промпта.</p></details>
<details class="criterion"><summary>Чек-лист</summary><p>Администратор меняет критерии, веса и правила; каждое сохранение — новая версия-черновик. «Активировать и пересчитать» включает версию и пересчитывает оценки без вызова модели. Удалённые критерии уходят в архив.</p></details>
<details class="criterion"><summary>Очередь проверки и точность</summary><p>«Очередь проверки» — ответы, где модель сама сомневается, и апелляции, ждущие решения. «Точность» — сравнение оценок с эталоном по каждому критерию.</p></details>
<details class="criterion"><summary>Цикл обучения</summary><p>Решения проверяющих складываются в закономерности с уверенностью (случаи, проверяющие, противоречия). Выше порога — оркестратор (ChatGPT · Codex) предлагает уточнение правила, администратор принимает или отклоняет. Новая версия проверяется на отложенных диалогах и активируется человеком. «До и после» показывает результат.</p></details>
<details class="criterion"><summary>Оценка и сдача</summary><p>Загрузка набора по group_id или из файла, оценка всего набора в фоне (каждый диалог — новая сессия агента без контекста), публикация и выгрузка results/dev или results/prod с проверкой формата.</p></details>
<details class="criterion"><summary>Оценка нового диалога</summary><p>Администратор или руководитель нажимает «Оценить диалог»: промпт собирается из активного чек-листа, ответ модели вставляется обратно, проверяется и публикуется.</p></details>`);
}
$('.skip').onclick = (e) => { e.preventDefault(); $('#main').focus(); };
$('#help').onclick = help;
$('#profile').onclick = help;
window.addEventListener('hashchange', route);
route();
load();
