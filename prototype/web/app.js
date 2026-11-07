const state = { bootstrap: null, conversation: null, evaluations: [], evaluation: null, selectedCriterion: null, checklistDraft: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
function safeHttpUrl(value) { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch { return null; } }

async function api(path, method = "GET", body) {
  const response = await fetch(path, { method, headers: body === undefined ? {} : {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || response.statusText);
  return data;
}
async function run(action) {
  try { $("#status").textContent = "Работаю…"; await action(); $("#status").textContent = "Готово"; }
  catch (error) { $("#status").textContent = "Ошибка: " + error.message; alert(error.message); }
}
async function reload() {
  state.bootstrap = await api("/api/bootstrap");
  $("#runBatch").textContent = state.bootstrap.provider === "mock" ? "Пакетный MOCK-прогон" : "Пакетная AI оценка";
  renderList();
}
function renderList() {
  const filter = $("#filter").value.toLowerCase();
  const items = state.bootstrap.conversations.filter((item) => (item.id + " " + item.language + " " + item.channel).toLowerCase().includes(filter));
  $("#conversationCount").textContent = "(" + state.bootstrap.conversations.length + ")";
  $("#conversationList").innerHTML = items.map((item) => '<button class="conversationItem ' + (state.conversation?.conversation_id === item.id ? "active" : "") + '" data-id="' + esc(item.id) + '"><b>' + esc(item.id) + '</b><small>' + esc(item.language) + " · " + esc(item.channel) + "</small></button>").join("");
  document.querySelectorAll(".conversationItem").forEach((button) => button.onclick = () => run(() => openConversation(button.dataset.id)));
}
async function openConversation(id) {
  const data = await api("/api/conversations/" + encodeURIComponent(id));
  state.conversation = data.conversation;
  state.evaluations = data.evaluations;
  state.evaluation = data.evaluations.length ? await api("/api/evaluations/" + data.evaluations[0].id) : null;
  state.selectedCriterion = null;
  renderList(); renderConversation(); renderEvaluation();
}
function renderConversation() {
  const c = state.conversation;
  if (!c) return;
  const audio = safeHttpUrl(c.audio_url);
  $("#conversationInfo").innerHTML = '<b>' + esc(c.conversation_id) + '</b> · ' + esc(c.language) + " · " + esc(c.channel) + " · " + c.transcript.length + " реплик" + (audio ? ' · <a href="' + esc(audio) + '" target="_blank" rel="noopener">Запись звонка ↗</a>' : "");
  $("#transcript").innerHTML = c.transcript.map((m) => '<div class="message ' + esc(m.speaker) + '" id="message-' + m.idx + '" data-idx="' + m.idx + '"><div class="messageMeta">#' + m.idx + " · " + esc(m.timestamp) + " · " + esc(m.speaker) + '</div><div class="messageText">' + esc(m.text) + "</div></div>").join("");
  document.querySelectorAll(".message").forEach((element) => element.onclick = () => {
    if (!state.selectedCriterion) return;
    const input = [...document.querySelectorAll(".criterionCard")].find((card) => card.dataset.criterion === state.selectedCriterion)?.querySelector(".evidenceInput");
    if (!input) return;
    const values = input.value.split(",").map((v) => Number(v.trim())).filter(Number.isInteger);
    const idx = Number(element.dataset.idx);
    input.value = [...new Set([...values, idx])].join(", ");
    element.classList.add("selected");
  });
}
function answerOptions(criterion, answer) {
  if (criterion.type === "scale") return criterion.scale.map((step) => '<option value="' + step.value + '" ' + (answer === step.value ? "selected" : "") + ">" + step.value + " — " + esc(step.text) + "</option>").join("");
  return '<option value="yes" ' + (answer === "yes" ? "selected" : "") + '>Да</option><option value="no" ' + (answer === "no" ? "selected" : "") + '>Нет</option>';
}
function evidenceLinks(indices) {
  return (Array.isArray(indices) ? indices : []).map((idx) => '<a class="evidenceLink" href="#message-' + esc(idx) + '" data-jump="' + esc(idx) + '">#' + esc(idx) + "</a>").join("");
}
function renderEvaluation() {
  const c = state.conversation;
  if (!c) return;
  const e = state.evaluation;
  $("#evaluationActions").innerHTML = '<select id="checklistVersion" aria-label="Версия чек-листа">' + state.bootstrap.versions.map((v) => '<option value="' + esc(v.id) + ':' + v.version + '">' + esc(v.id) + ' v' + v.version + '</option>').join("") + '</select><button id="newEvaluation" class="primary">Новая оценка</button>' + (state.evaluations.length ? '<select id="evaluationHistory" aria-label="История оценок">' + state.evaluations.map((item) => '<option value="' + esc(item.id) + '" ' + (item.id === e?.id ? "selected" : "") + '>' + esc(item.created_at.slice(0, 16)) + ' · v' + item.checklist_version + ' · ' + esc(item.source) + '</option>').join("") + '</select>' : "") + (e ? '<button id="aiDraft" ' + (!state.bootstrap.ai_configured ? 'title="Настройте ключ и модель"' : "") + '>' + (state.bootstrap.provider === "mock" ? "MOCK: заполнить" : "AI: один проход") + '</button>' : "");
  $("#newEvaluation").onclick = () => run(async () => { const [checklist_id, version] = $("#checklistVersion").value.split(":"); await api("/api/evaluations", "POST", {conversation_id: c.conversation_id, checklist_id, checklist_version:Number(version)}); await openConversation(c.conversation_id); });
  if (state.evaluations.length) $("#evaluationHistory").onchange = () => run(async () => { state.evaluation = await api("/api/evaluations/" + $("#evaluationHistory").value); renderEvaluation(); });
  if (e) $("#aiDraft").onclick = () => run(async () => { state.evaluation = await api("/api/evaluations/" + e.id + "/ai", "POST"); const item = state.evaluations.find((value) => value.id === e.id); if (item) item.source = state.evaluation.source; renderEvaluation(); });
  if (!e) { $("#pipeline").innerHTML = ""; $("#evaluation").innerHTML = '<div class="card">Создайте оценку. Можно начать вручную или заполнить все критерии одним вызовом AI.</div>'; return; }
  const checklist = e.checklist_snapshot;
  const answers = e.answers;
  const has = (field) => checklist.criteria.every((criterion) => {
    const a = answers[criterion.id];
    if (field === "applicability") return a.applicability === "applicable" || a.applicability === "na";
    if (field === "evidence") return Array.isArray(a.evidence_idx) && a.evidence_idx.length;
    return a.answer !== null || a.applicability === "na";
  });
  const steps = [
    ["1 Вход", true], ["2 Применимость", has("applicability")], ["3 Доказательства", has("evidence")],
    ["4 Оценка", has("answer")], ["5 Проверка", !e.validation_errors.length], ["6 Итог", !!e.score],
    ["7 Апелляции", e.appeals.length > 0], ["8 История", e.events.length > 0]
  ];
  $("#pipeline").innerHTML = steps.map(([title, done]) => '<span class="step ' + (done ? "done" : "warn") + '">' + title + "</span>").join("");
  let html = '<div class="card"><b>Оценка ' + esc(e.id.slice(0, 8)) + '</b> · чек-лист ' + esc(e.checklist_id) + ' v' + e.checklist_version + ' · источник: ' + esc(e.source) + (e.source === "mock-technical-demo" ? '<p class="error"><b>MOCK — техническая заглушка. Эти ответы не являются оценкой качества или измерением точности AI.</b></p>' : "") + '<p class="muted">AI заполняет применимость, доказательства и оценку одним вызовом. Все поля ниже можно исправить; проверка и пересчёт запускаются после сохранения.</p><button id="showPrompt">Показать точный запрос к модели</button><details id="promptPanel"><summary>Вход модели</summary><pre></pre></details></div>';
  if (e.score) {
    html += '<div class="card"><h3>6 · Детерминированный итог' + (e.source === "mock-technical-demo" ? " (MOCK-данные)" : "") + '</h3><div class="score">' + (e.score.total === null ? "не рассчитывается" : e.score.total + " / 100") + '</div><p>Применимые веса нормируются до 100. N/A исключены. Грубые ошибки: ' + Object.entries(e.score.flags).map(([id, flag]) => id + " " + (flag ? "Да" : "Нет")).join(", ") + '</p><div class="scoreGrid">' + Object.entries(e.score.sections).map(([id, value]) => "<div>" + esc(checklist.sections.find((s) => s.id === id)?.title || id) + ": <b>" + (value === null ? "—" : value) + "</b></div>").join("") + '</div><p class="muted">Баллы по критериям и эффективный вес показаны ниже.</p></div>';
  } else {
    html += '<div class="card"><h3>5 · Строгая проверка</h3><p>Итог пока не рассчитывается. Исправьте поля:</p><div class="error">' + e.validation_errors.map(esc).join("<br>") + "</div></div>";
  }
  if (e.baseline_score || e.baseline_errors.length) html += '<div class="card"><h3>8 · Рефлексия относительно последнего прохода</h3><p>Baseline: <b>' + (e.baseline_score?.total ?? "не прошёл проверку") + '</b> · текущий итог: <b>' + (e.score?.total ?? "не рассчитан") + '</b></p><p>Изменены критерии: ' + (e.changed_criteria.length ? e.changed_criteria.map(esc).join(", ") : "нет") + '</p>' + (e.baseline_errors.length ? '<p class="error">' + e.baseline_errors.map(esc).join("; ") + '</p>' : "") + '</div>';
  checklist.criteria.forEach((criterion) => {
    const a = answers[criterion.id];
    const row = e.score?.rows.find((r) => r.criterion_id === criterion.id);
    html += '<div class="card criterionCard" data-criterion="' + esc(criterion.id) + '"><div class="cardHead"><h3>' + esc(criterion.id) + " · " + esc(criterion.title) + '</h3><span class="chip">вес ' + criterion.weight + "</span></div><p>" + esc(criterion.description) + '</p><p class="muted">N/A: ' + (criterion.na_allowed ? esc(criterion.na_condition) : "запрещён") + "</p>";
    if (row) html += '<p><b>' + (row.raw_points === null ? "N/A" : row.raw_points + " баллов") + '</b> · эффективный вес ' + (row.effective_weight ?? "—") + "%</p>";
    html += '<div class="fields"><label>2 · Применимость<select class="applicability"><option value="" ' + (!a.applicability ? "selected" : "") + '>Не выбрана</option><option value="applicable" ' + (a.applicability === "applicable" ? "selected" : "") + '>Применим</option>' + (criterion.na_allowed ? '<option value="na" ' + (a.applicability === "na" ? "selected" : "") + '>N/A</option>' : "") + '</select></label>';
    html += '<label>4 · Точная ступень<select class="answer"><option value="" ' + (a.answer === null ? "selected" : "") + '>Не выбрана</option>' + answerOptions(criterion, a.answer) + "</select></label>";
    html += '<label class="wide">Причина N/A (если выбрано)<input class="naReason" value="' + esc(a.na_reason || "") + '"></label>';
    html += '<label class="wide">3 · Индексы evidence / проверенный участок (через запятую)<input class="evidenceInput" value="' + esc(Array.isArray(a.evidence_idx) ? a.evidence_idx.join(", ") : "") + '"></label>';
    html += '<label class="wide">4 · Обоснование<textarea class="explanation">' + esc(a.explanation || "") + "</textarea></label></div>";
    const openAppeal = e.appeals.some((appeal) => appeal.criterion_id === criterion.id && appeal.status === "open");
    html += '<div class="cardActions"><button class="saveAnswer primary" ' + (openAppeal ? "disabled" : "") + '>Сохранить и пересчитать</button><span class="muted">' + (openAppeal ? "Решение по апелляции читает эти поля без предварительного сохранения" : "Нажмите на реплику слева для добавления evidence") + '</span></div><div>' + evidenceLinks(a.evidence_idx) + "</div>";
    (Array.isArray(a.evidence_idx) ? a.evidence_idx : []).forEach((idx) => { const message = e.conversation.transcript[idx - 1]; if (message && message.idx === idx) html += '<p class="evidenceQuote">#' + esc(idx) + ' [' + esc(message.timestamp) + '] ' + esc(message.speaker) + ': ' + esc(message.text) + '</p>'; });
    html += "</div>";
  });
  html += '<div class="card"><h3>7 · Апелляции</h3><select id="appealCriterion">' + checklist.criteria.map((criterion) => '<option value="' + esc(criterion.id) + '">' + esc(criterion.title) + "</option>").join("") + '</select><textarea id="appealComment" placeholder="Что оспаривается и почему?"></textarea><button id="sendAppeal">Подать апелляцию</button>';
  e.appeals.forEach((appeal) => {
    const opening = e.events.find((event) => event.kind === "appeal_opened" && event.detail.appeal_id === appeal.id);
    html += '<div class="card"><b>' + esc(appeal.criterion_id) + " · " + esc(appeal.status) + '</b><p>Комментарий: ' + esc(appeal.comment) + '</p><p>Исходное решение: ' + esc(JSON.stringify(opening?.detail.original_answer || {})) + ' · исходный итог ' + esc(opening?.detail.original_total ?? "—") + '</p>';
    if (appeal.decision) html += '<p>Решение: ' + esc(appeal.decision.action) + " · " + esc(appeal.decision.reason) + "</p>";
    else html += '<div class="cardActions"><button class="confirmAppeal" data-aid="' + appeal.id + '">Подтвердить</button><button class="changeAppeal" data-aid="' + appeal.id + '">Изменить на поля критерия выше</button></div>';
    html += "</div>";
  });
  html += '</div><div class="card"><h3>8 · Журнал изменений</h3><div class="history">' + e.events.slice().reverse().map((event) => '<div>' + esc(event.created_at) + " · " + esc(event.kind) + " · " + esc(event.actor) + (event.detail.criterion_id ? " · " + esc(event.detail.criterion_id) : "") + (event.kind === "ai_draft" ? " · prompt " + esc(event.detail.prompt_version) + " · " + esc(event.detail.duration_seconds) + "s · tokens " + esc(JSON.stringify(event.detail.usage || {})) : "") + "</div>").join("") + "</div></div>";
  $("#evaluation").innerHTML = html;
  $("#showPrompt").onclick = () => run(async () => {
    const preview = await api("/api/evaluations/" + e.id + "/prompt");
    $("#promptPanel pre").textContent = JSON.stringify(preview, null, 2);
    $("#promptPanel").open = true;
  });
  document.querySelectorAll(".criterionCard").forEach((card) => {
    card.onclick = () => { state.selectedCriterion = card.dataset.criterion; };
    card.querySelector(".saveAnswer").onclick = () => run(async () => {
      const answer = readAnswerCard(card);
      state.evaluation = await api("/api/evaluations/" + e.id + "/answers/" + card.dataset.criterion, "PATCH", answer);
      renderEvaluation();
    });
  });
  document.querySelectorAll("[data-jump]").forEach((link) => link.onclick = () => {
    const message = document.querySelector("#message-" + link.dataset.jump);
    if (message) { message.scrollIntoView({behavior:"smooth", block:"center"}); message.classList.add("selected"); }
  });
  $("#sendAppeal").onclick = () => run(async () => {
    state.evaluation = await api("/api/evaluations/" + e.id + "/appeals", "POST", {criterion_id: $("#appealCriterion").value, comment: $("#appealComment").value});
    renderEvaluation();
  });
  document.querySelectorAll(".confirmAppeal").forEach((button) => button.onclick = () => decide(button.dataset.aid, "confirm"));
  document.querySelectorAll(".changeAppeal").forEach((button) => button.onclick = () => decide(button.dataset.aid, "change"));
}
function readAnswerCard(card) {
  const criterion = state.evaluation.checklist_snapshot.criteria.find((item) => item.id === card.dataset.criterion);
  const applicability = card.querySelector(".applicability").value || null;
  const rawAnswer = card.querySelector(".answer").value;
  const answer = applicability === "na" || !rawAnswer ? null : criterion.type === "scale" ? Number(rawAnswer) : rawAnswer;
  const evidence_idx = card.querySelector(".evidenceInput").value.split(",").map((value) => value.trim()).filter(Boolean).map(Number);
  return {applicability, answer, na_reason: card.querySelector(".naReason").value, evidence_idx, explanation: card.querySelector(".explanation").value};
}
function decide(aid, action) {
  const reason = prompt("Причина решения проверяющего:");
  if (!reason) return;
  run(async () => {
    const appeal = state.evaluation.appeals.find((item) => item.id === aid);
    const card = [...document.querySelectorAll(".criterionCard")].find((element) => element.dataset.criterion === appeal.criterion_id);
    const answer = readAnswerCard(card);
    state.evaluation = await api("/api/appeals/" + aid + "/decision", "POST", {action, reason, answer: action === "change" ? answer : undefined});
    renderEvaluation();
  });
}
function renderChecklist() {
  const draft = state.checklistDraft;
  const ids = [...new Set(state.bootstrap.versions.map((item) => item.id))];
  $("#checklistEditor").innerHTML = '<label>Открыть существующий <select id="editChecklistPicker">' + ids.map((id) => '<option value="' + esc(id) + '" ' + (id === draft.id ? "selected" : "") + '>' + esc(id) + '</option>').join("") + '</select></label><p><label>ID (измените для нового чек-листа) <input id="checklistId" value="' + esc(draft.id) + '"></label> <label>Название <input id="checklistTitle" value="' + esc(draft.title) + '"></label></p><p>Текущая версия: ' + draft.version + '</p><label>Общие правила<textarea id="globalRules">' + esc((draft.global_rules || []).join("\n")) + '</textarea></label>' + draft.criteria.map((criterion, index) => '<div class="criterionEditor" data-index="' + index + '"><input class="cid" value="' + esc(criterion.id) + '" title="ID"><input class="title" value="' + esc(criterion.title) + '" title="Название"><input class="weight" type="number" min="0" value="' + criterion.weight + '" title="Вес"><select class="type"><option value="binary" ' + (criterion.type === "binary" ? "selected" : "") + '>Да/Нет</option><option value="scale" ' + (criterion.type === "scale" ? "selected" : "") + '>Шкала</option><option value="flag" ' + (criterion.type === "flag" ? "selected" : "") + '>Флаг</option></select><label><input class="naAllowed" type="checkbox" ' + (criterion.na_allowed ? "checked" : "") + '> N/A</label><input class="wide description" value="' + esc(criterion.description || "") + '" placeholder="Описание"><input class="wide naCondition" value="' + esc(criterion.na_condition || "") + '" placeholder="Условие N/A"><input class="wide scale" value="' + esc((criterion.scale || []).map((step) => step.value + ":" + step.text).join(" | ")) + '" placeholder="Шкала: 0:Описание | 5:Описание"><select class="section">' + draft.sections.map((section) => '<option value="' + esc(section.id) + '" ' + (criterion.section === section.id ? "selected" : "") + '>' + esc(section.title) + '</option>').join("") + '</select><button class="removeCriterion danger">Удалить</button></div>').join("");
  $("#editChecklistPicker").onchange = () => run(async () => { state.checklistDraft = await api("/api/checklists/" + encodeURIComponent($("#editChecklistPicker").value)); renderChecklist(); });
  document.querySelectorAll(".removeCriterion").forEach((button) => button.onclick = () => { readChecklist(); draft.criteria.splice(Number(button.parentElement.dataset.index), 1); renderChecklist(); });
}
function readChecklist() {
  const draft = state.checklistDraft;
  draft.id = $("#checklistId").value.trim();
  draft.title = $("#checklistTitle").value;
  draft.global_rules = $("#globalRules").value.split("\n").map((line) => line.trim()).filter(Boolean);
  draft.criteria = [...document.querySelectorAll(".criterionEditor")].map((element) => {
    const type = element.querySelector(".type").value;
    const scaleText = element.querySelector(".scale").value;
    const scale = scaleText.split("|").map((part) => part.trim()).filter(Boolean).map((part) => { const index = part.indexOf(":"); return {value:Number(part.slice(0,index)), text:part.slice(index+1).trim()}; });
    return {id:element.querySelector(".cid").value.trim(), section:element.querySelector(".section").value, title:element.querySelector(".title").value.trim(), description:element.querySelector(".description").value.trim(), weight:Number(element.querySelector(".weight").value), type, na_allowed:element.querySelector(".naAllowed").checked, na_condition:element.querySelector(".naCondition").value.trim() || null, ...(type === "scale" ? {scale} : {})};
  });
}
$("#importDev").onclick = () => run(async () => { await api("/api/import-dev", "POST"); await reload(); });
$("#importFile").onchange = (event) => run(async () => {
  const text = await event.target.files[0].text();
  const data = JSON.parse(text);
  const records = data?.data?.records || data.records || (Array.isArray(data) ? data : [data]);
  await api("/api/import", "POST", {records, group_id:data?.data?.dataset_group_id || "UPLOAD"});
  await reload();
});
$("#importGroup").onclick = () => run(async () => { await api("/api/import-group", "POST", {group_id:$("#groupId").value.trim()}); await reload(); });
$("#runBatch").onclick = () => run(async () => {
  const ids = state.bootstrap.conversations.filter((item) => (item.id + " " + item.language + " " + item.channel).toLowerCase().includes($("#filter").value.toLowerCase())).map((item) => item.id);
  if (!ids.length) throw new Error("Сначала загрузите диалоги");
  const job = await api("/api/batch", "POST", {conversation_ids:ids});
  $("#batchStatus").textContent = "0 / " + job.total;
  const timer = setInterval(async () => {
    try {
      const progress = await api("/api/batch/" + job.id);
      $("#batchStatus").textContent = progress.done + " / " + progress.total + " · " + progress.results.filter((item) => item.status === "valid").length + " прошли проверку схемы" + (state.bootstrap.provider === "mock" ? " (MOCK)" : "") + " · " + progress.results.filter((item) => item.status === "needs_review").length + " требуют проверки · " + progress.results.filter((item) => item.status === "error").length + " ошибок";
      if (progress.status === "complete") { clearInterval(timer); if (state.conversation) await openConversation(state.conversation.conversation_id); }
    } catch (error) { clearInterval(timer); $("#batchStatus").textContent = error.message; }
  }, 1500);
});
$("#filter").oninput = renderList;
$("#openChecklist").onclick = () => { state.checklistDraft = structuredClone(state.bootstrap.checklist); renderChecklist(); $("#checklistDialog").showModal(); };
$("#closeChecklist").onclick = () => $("#checklistDialog").close();
$("#addCriterion").onclick = () => { readChecklist(); state.checklistDraft.criteria.push({id:"new-" + (state.checklistDraft.criteria.length+1), section:state.checklistDraft.sections[0].id, title:"Новый критерий", description:"", type:"binary", weight:10, na_allowed:false, na_condition:null}); renderChecklist(); };
$("#saveChecklist").onclick = () => run(async () => { readChecklist(); state.bootstrap.checklist = await api("/api/checklists", "POST", state.checklistDraft); await reload(); $("#checklistDialog").close(); });
run(reload);
