"use strict";

/* ------------------------------------------------------------------ утилиты */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const time = (ts) => (ts || "").slice(11, 16);

const STATUS = {
  new: "новый", running: "в работе", waiting: "ждёт ответа", paused: "пауза",
  solved: "решён", failed: "не решён", stopped: "остановлен",
};
const pill = (status) => `<span class="pill ${esc(status)}">${esc(STATUS[status] || status)}</span>`;

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(path, {
    method,
    headers: { "X-Token": APP.token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) { location.reload(); throw new Error("Сессия устарела"); }
  const data = resp.headers.get("Content-Type")?.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) throw new Error((data && data.error) || `Ошибка ${resp.status}`);
  return data;
}

let toastTimer;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), error ? 6000 : 3000);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ состояние */

const S = {
  tickets: [],
  state: { active_ticket: null },
  ticketId: null,
  ticket: null,
  lastEventId: 0,
  prompts: {},
  planStep: null,
  pendingId: null,
};

/* ------------------------------------------------------------------ список тикетов */

async function loadTickets() {
  const q = encodeURIComponent($("#search").value.trim());
  const st = encodeURIComponent($("#status-filter").value);
  S.tickets = await api(`/api/tickets?q=${q}&status=${st}`);
  renderList();
}

function renderList() {
  const list = $("#ticket-list");
  if (!S.tickets.length) {
    list.innerHTML = `<p class="muted" style="padding:8px">Тикетов пока нет</p>`;
    return;
  }
  list.innerHTML = S.tickets.map((t) => `
    <a class="ticket-item ${t.id === S.ticketId ? "active" : ""}" href="#/t/${t.id}">
      <div class="row"><span class="title">${esc(t.title)}</span>${pill(t.status)}</div>
      <div class="meta">#${t.id} · ${esc(t.anydesk_id || "без AnyDesk")} · ${esc(t.updated_at.replace("T", " ").slice(0, 16))}</div>
    </a>`).join("");
}

async function loadState() {
  S.state = await api("/api/state");
  $("#version").textContent = "v" + S.state.version;
  const badge = $("#update-badge");
  if (S.state.update) {
    badge.textContent = `Доступна v${S.state.update.version}`;
    badge.classList.remove("hidden");
  } else badge.classList.add("hidden");
  const active = $("#active-session");
  if (S.state.active_ticket) {
    active.innerHTML = `<a href="#/t/${S.state.active_ticket}">● Идёт сеанс: тикет #${S.state.active_ticket}</a>`;
    active.classList.remove("hidden");
  } else active.classList.add("hidden");
}

/* ------------------------------------------------------------------ роутинг */

function route() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/t\/(\d+)/);
  S.ticketId = m ? Number(m[1]) : null;
  $("#main").scrollTop = 0;
  renderList();
  if (m) return viewTicket(S.ticketId);
  if (hash.startsWith("#/new")) return viewNew();
  if (hash.startsWith("#/settings")) return viewSettings();
  return viewEmpty();
}

function viewEmpty() {
  $("#main").innerHTML = `
    <div class="empty">
      <h2>ИИ-агент техподдержки через AnyDesk</h2>
      <p>Создайте тикет: опишите проблему пользователя и укажите его ID AnyDesk.<br>
      Агент подключится, задаст вопросы, составит план и решит задачу через консоль PowerShell.</p>
      <p><a class="btn primary" href="#/new">+ Новый тикет</a></p>
    </div>`;
}

/* ------------------------------------------------------------------ новый тикет */

function viewNew() {
  $("#main").innerHTML = `
    <h1 style="margin-bottom:16px">Новый тикет</h1>
    <form class="form card" id="new-form">
      <div class="form-row">
        <label class="field">Что нужно сделать
          <input name="title" required maxlength="200" placeholder="Установить Microsoft Office" autofocus>
        </label>
        <label class="field">ID AnyDesk пользователя
          <input name="anydesk_id" placeholder="123 456 789" inputmode="numeric">
        </label>
      </div>
      <label class="field">Описание проблемы
        <span class="hint">Чем подробнее, тем лучше: что случилось, что уже пробовали, какие пожелания у пользователя (версия, язык, лицензия…)</span>
        <textarea name="description" rows="6" placeholder="Пользователь просит поставить Word и Excel. Лицензия Microsoft 365 для бизнеса, учётка есть. Windows 10."></textarea>
      </label>
      <div class="actions">
        <button class="btn primary" type="submit">Создать тикет</button>
        <a class="btn ghost" href="#/">Отмена</a>
      </div>
    </form>`;
  $("#new-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    guarded(async () => {
      const t = await api("/api/tickets", { method: "POST", body: Object.fromEntries(f) });
      await loadTickets();
      location.hash = `#/t/${t.id}`;
    });
  });
}

/* ------------------------------------------------------------------ тикет */

async function viewTicket(id) {
  S.ticket = null;
  S.lastEventId = 0;
  S.prompts = {};
  S.planStep = null;
  S.pendingId = null;
  $("#main").innerHTML = `<p class="muted">Загрузка…</p>`;
  const t = await guarded(() => api(`/api/tickets/${id}`));
  if (!t || S.ticketId !== id) return;
  S.ticket = t;
  $("#main").innerHTML = `
    <div class="ticket-head">
      <div class="info" id="t-info"></div>
      <div class="actions" id="t-actions"></div>
    </div>
    <div id="t-edit" class="hidden"></div>
    <div class="ticket-grid">
      <div>
        <div class="timeline" id="timeline"></div>
        <div id="pending" style="margin-top:12px"></div>
      </div>
      <div class="side-panel">
        <div class="card" id="start-card"></div>
        <div class="card"><h3>План</h3><div id="plan"></div></div>
        <div class="card">
          <h3 id="hint-title">Подсказка агенту</h3>
          <textarea id="hint-text" rows="3" placeholder="Например: не удаляй старый Office, только обнови"></textarea>
          <div class="actions" style="margin-top:8px"><button class="btn small" id="hint-send">Отправить</button></div>
        </div>
        <div class="card"><h3>Промт для ИИ</h3><div id="prompts" class="muted" style="font-size:13px">Появится после запуска</div></div>
      </div>
    </div>`;
  $("#hint-send").addEventListener("click", sendHint);
  renderTicketMeta();
  await pollEvents();
}

function renderTicketMeta() {
  const t = S.ticket;
  const sess = t.session || {};
  $("#t-info").innerHTML = `
    <h1>${esc(t.title)}</h1>
    <div class="meta">
      <span>#${t.id}</span> ${pill(t.status)}
      <span>AnyDesk: <b class="mono">${esc(t.anydesk_id || "—")}</b></span>
      <span>создан ${esc(t.created_at.replace("T", " ").slice(0, 16))}</span>
    </div>
    ${t.description ? `<p class="description">${esc(t.description)}</p>` : ""}
    ${t.summary ? `<p class="description"><b>Итог:</b> ${esc(t.summary)}</p>` : ""}`;

  const acts = [];
  if (sess.active) {
    acts.push(sess.paused
      ? `<button class="btn" data-ctl="resume">▶ Продолжить</button>`
      : `<button class="btn" data-ctl="pause">⏸ Пауза</button>`);
    acts.push(`<button class="btn danger" data-ctl="stop">■ Стоп</button>`);
  } else {
    acts.push(`<button class="btn ghost" id="edit-btn">Изменить</button>`);
    acts.push(`<button class="btn ghost danger" id="delete-btn">Удалить</button>`);
  }
  acts.push(`<a class="btn ghost" href="/api/tickets/${t.id}/export?t=${encodeURIComponent(APP.token)}">Экспорт отчёта</a>`);
  $("#t-actions").innerHTML = acts.join("");
  $("#t-actions").querySelectorAll("[data-ctl]").forEach((b) =>
    b.addEventListener("click", () => control(b.dataset.ctl)));
  $("#edit-btn")?.addEventListener("click", showEdit);
  $("#delete-btn")?.addEventListener("click", deleteTicket);

  renderStartCard();
  renderPlan();
  renderPending();
  $("#hint-title").textContent = sess.active ? "Подсказка агенту" : "Заметка к тикету";
}

function renderStartCard() {
  const t = S.ticket;
  const card = $("#start-card");
  const busy = S.state.active_ticket && S.state.active_ticket !== t.id;
  // Перерисовываем только при смене состояния — иначе сотрём введённый пароль.
  const key = `${t.id}|${!!t.session?.active}|${busy}|${t.status === "new"}`;
  if (card.dataset.key === key) return;
  card.dataset.key = key;
  if (t.session?.active) {
    card.innerHTML = `<h3>Сеанс идёт</h3><p class="muted" style="margin:6px 0 0">Агент работает. Не трогайте мышь и клавиатуру этого ПК —
      агент управляет окном AnyDesk. Аварийная остановка: мышь в левый верхний угол экрана.</p>`;
    return;
  }
  card.innerHTML = `
    <h3>${t.status === "new" ? "Запуск агента" : "Запустить заново"}</h3>
    <div class="start-box" style="margin-top:10px">
      ${APP.dev ? `
        <label class="field">Режим
          <select id="st-mode"><option value="anydesk">AnyDesk</option><option value="local">Этот ПК (тест)</option></select>
        </label>
        <label class="check"><input type="checkbox" id="st-dry"> Без нажатий (dry-run)</label>` : ""}
      <label class="check"><input type="checkbox" id="st-connect" checked> Подключиться к ${esc(t.anydesk_id || "?")} автоматически</label>
      <label class="field">Пароль неконтролируемого доступа
        <span class="hint">Необязательно. Без пароля пользователь сам примет подключение. Пароль не сохраняется.</span>
        <input type="password" id="st-password" autocomplete="off">
      </label>
      <button class="btn primary" id="st-start" ${busy ? "disabled" : ""}>▶ Запустить агента</button>
      ${busy ? `<span class="muted">Сейчас идёт сеанс по тикету #${S.state.active_ticket}</span>` : ""}
    </div>`;
  $("#st-start").addEventListener("click", () => guarded(async () => {
    await api(`/api/tickets/${t.id}/start`, {
      method: "POST",
      body: {
        password: $("#st-password").value,
        connect: $("#st-connect").checked,
        mode: $("#st-mode")?.value || "anydesk",
        dry_run: $("#st-dry")?.checked || false,
      },
    });
    toast("Агент запущен");
    await refreshTicket();
    loadState();
    loadTickets();
  }));
}

function renderPlan() {
  const plan = S.ticket.plan || [];
  $("#plan").innerHTML = plan.length
    ? `<ol class="plan-list">${plan.map((p, i) => {
        const n = i + 1;
        const cls = S.planStep === n ? "current" : (S.planStep && n < S.planStep ? "past" : "");
        return `<li class="${cls}">${esc(p)}</li>`;
      }).join("")}</ol>`
    : `<p class="muted" style="margin:6px 0 0">ИИ составит план после запуска</p>`;
}

/* ------------------------------------------------------------------ вопрос ИИ (ожидает ответа) */

const ASK_LABEL = {
  question: "ИИ спрашивает", plan: "Подтвердите план", confirm: "Подтвердите действие",
  done: "Задача решена?", fail: "ИИ не справляется", error: "Ошибка", pick_window: "Нужна помощь",
};

function renderPending() {
  const box = $("#pending");
  const p = S.ticket.session?.pending;
  if (!p) { box.innerHTML = ""; S.pendingId = null; return; }
  if (S.pendingId === p.id) return; // не стираем то, что техник уже печатает
  S.pendingId = p.id;
  const d = p.data || {};
  let body = "";
  let buttons = "";
  switch (p.kind) {
    case "plan":
      body = `<ol class="plan-list">${(d.plan || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        <textarea id="ans-text" rows="2" placeholder="Поправка к плану (необязательно)"></textarea>`;
      buttons = `<button class="btn green" data-btn="yes">Начать</button>
        <button class="btn" data-send>Отправить поправку</button>
        <button class="btn danger" data-btn="stop">Стоп</button>`;
      break;
    case "confirm":
      body = `${d.command ? `<pre>${esc(d.command)}</pre>` : `<pre>${esc(p.text)}</pre>`}
        ${d.risky ? `<span class="pill risky">ИИ пометил как рискованное</span>` : ""}
        <input id="ans-text" placeholder="Или напишите, что сделать вместо этого">`;
      buttons = `<button class="btn green" data-btn="yes">Выполнить</button>
        <button class="btn" data-btn="no">Пропустить</button>
        <button class="btn" data-send>Отклонить с подсказкой</button>
        <button class="btn danger" data-btn="stop">Стоп</button>`;
      break;
    case "done":
      body = `<input id="ans-text" placeholder="Если не решено — напишите, что не так">`;
      buttons = `<button class="btn green" data-btn="yes">Да, решено</button>
        <button class="btn" data-send>Нет, продолжить</button>`;
      break;
    case "fail":
      body = `<textarea id="ans-text" rows="2" placeholder="Подсказка, как решить"></textarea>`;
      buttons = `<button class="btn" data-send>Продолжить с подсказкой</button>
        <button class="btn danger" data-btn="stop">Завершить</button>`;
      break;
    case "error":
      buttons = `<button class="btn" data-btn="yes">Повторить</button>
        <button class="btn danger" data-btn="stop">Стоп</button>`;
      break;
    case "pick_window":
      buttons = `<button class="btn primary" data-btn="yes">Выбрать окно (5 сек)</button>
        <button class="btn danger" data-btn="stop">Стоп</button>`;
      break;
    default:
      body = `<textarea id="ans-text" rows="2" placeholder="Ваш ответ"></textarea>`;
      buttons = `<button class="btn primary" data-send>Ответить</button>
        <button class="btn danger" data-btn="stop">Стоп</button>`;
  }
  box.innerHTML = `
    <div class="pending">
      <div class="label">${esc(ASK_LABEL[p.kind] || "Вопрос")}</div>
      ${p.kind === "confirm" && d.command ? "" : `<div class="q">${esc(p.text)}</div>`}
      ${body}
      <div class="actions">${buttons}</div>
    </div>`;
  box.querySelectorAll("[data-btn]").forEach((b) =>
    b.addEventListener("click", () => answer(p.id, b.dataset.btn, true)));
  box.querySelectorAll("[data-send]").forEach((b) =>
    b.addEventListener("click", () => {
      const text = $("#ans-text")?.value.trim();
      if (!text && p.kind !== "question") return toast("Напишите текст", true);
      answer(p.id, text || "", false);
    }));
  const input = $("#ans-text");
  if (input) {
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && input.tagName === "INPUT") {
        e.preventDefault();
        box.querySelector("[data-send]")?.click();
      }
      if (e.key === "Enter" && e.ctrlKey) box.querySelector("[data-send]")?.click();
    });
  }
  box.scrollIntoView({ block: "nearest" });
}

async function answer(askId, value, button) {
  await guarded(async () => {
    await api(`/api/tickets/${S.ticketId}/answer`, { method: "POST", body: { ask_id: askId, value, button } });
    $("#pending").innerHTML = "";
    await refreshTicket();
    loadTickets();
  });
}

async function control(cmd) {
  if (cmd === "stop" && !confirm("Остановить агента?")) return;
  await guarded(async () => {
    await api(`/api/tickets/${S.ticketId}/control`, { method: "POST", body: { cmd } });
    await refreshTicket();
  });
}

async function sendHint() {
  const text = $("#hint-text").value.trim();
  if (!text) return;
  await guarded(async () => {
    await api(`/api/tickets/${S.ticketId}/hint`, { method: "POST", body: { text } });
    $("#hint-text").value = "";
    toast(S.ticket.session?.active ? "Подсказка передана агенту" : "Заметка добавлена");
    await pollEvents();
  });
}

function showEdit() {
  const t = S.ticket;
  const box = $("#t-edit");
  box.classList.remove("hidden");
  box.innerHTML = `
    <form class="form card" id="edit-form" style="margin-bottom:16px">
      <div class="form-row">
        <label class="field">Что нужно сделать<input name="title" value="${esc(t.title)}" required></label>
        <label class="field">ID AnyDesk<input name="anydesk_id" value="${esc(t.anydesk_id)}"></label>
      </div>
      <label class="field">Описание<textarea name="description" rows="4">${esc(t.description)}</textarea></label>
      <label class="field">Статус
        <select name="status">${Object.entries(STATUS).filter(([k]) => !["running", "waiting", "paused"].includes(k))
          .map(([k, v]) => `<option value="${k}" ${k === t.status ? "selected" : ""}>${v}</option>`).join("")}</select>
      </label>
      <div class="actions"><button class="btn primary">Сохранить</button>
        <button type="button" class="btn ghost" id="edit-cancel">Отмена</button></div>
    </form>`;
  $("#edit-cancel").addEventListener("click", () => box.classList.add("hidden"));
  $("#edit-form").addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(async () => {
      await api(`/api/tickets/${t.id}`, { method: "PATCH", body: Object.fromEntries(new FormData(e.target)) });
      box.classList.add("hidden");
      await refreshTicket();
      loadTickets();
    });
  });
}

async function deleteTicket() {
  if (!confirm(`Удалить тикет #${S.ticketId} вместе с логами?`)) return;
  await guarded(async () => {
    await api(`/api/tickets/${S.ticketId}`, { method: "DELETE" });
    await loadTickets();
    location.hash = "#/";
  });
}

/* ------------------------------------------------------------------ лента событий */

async function refreshTicket() {
  if (!S.ticketId) return;
  const t = await api(`/api/tickets/${S.ticketId}`);
  if (t.id !== S.ticketId) return;
  S.ticket = t;
  renderTicketMeta();
  await pollEvents();
}

async function pollEvents() {
  const id = S.ticketId;
  const events = await api(`/api/tickets/${id}/events?after=${S.lastEventId}`);
  if (id !== S.ticketId || !$("#timeline")) return;
  const tl = $("#timeline");
  const nearBottom = $("#main").scrollHeight - $("#main").scrollTop - $("#main").clientHeight < 200;
  for (const e of events) {
    S.lastEventId = e.id;
    if (e.kind === "prompt") { S.prompts[e.text] = e.data; renderPrompts(); continue; }
    if (e.kind === "step" && e.data.plan_step) S.planStep = e.data.plan_step;
    if (e.kind === "plan") S.ticket.plan = e.data.plan || [];
    const html = renderEvent(e);
    if (html) tl.insertAdjacentHTML("beforeend", html);
  }
  if (events.length) {
    renderPlan();
    if (nearBottom) $("#main").scrollTop = $("#main").scrollHeight;
  }
}

function renderPrompts() {
  $("#prompts").innerHTML = Object.entries(S.prompts).map(([title, d]) => `
    <details style="margin-top:6px"><summary>${esc(title)}</summary>
      <p class="muted" style="margin:6px 0 2px">Системный промт (инструкция и формат ответа):</p><pre>${esc(d.system)}</pre>
      <p class="muted" style="margin:6px 0 2px">Сообщение с данными тикета:</p><pre>${esc(d.user)}</pre>
    </details>`).join("");
}

function renderEvent(e) {
  const d = e.data || {};
  const t = `<span class="time">${time(e.ts)}</span>`;
  switch (e.kind) {
    case "status":
    case "info":
      return `<div class="ev ev-line">${t}<span>${esc(e.text)}</span></div>`;
    case "warning":
      return `<div class="ev ev-warning">${t} ⚠ ${esc(e.text)}</div>`;
    case "error":
      return `<div class="ev ev-error">${t} ✘ ${esc(e.text)}
        ${d.traceback ? `<details><summary>Подробности</summary><pre>${esc(d.traceback)}</pre></details>` : ""}</div>`;
    case "plan":
      return `<div class="ev ev-plan">${t} <b>План готов.</b> ${esc(e.text)}</div>`;
    case "ask":
      return `<div class="ev ev-ask">${t} <b>❓ ${esc(ASK_LABEL[d.ask_kind] || "Вопрос")}:</b> ${esc(d.ask_kind === "confirm" && d.command ? d.command : e.text)}</div>`;
    case "answer":
      return `<div class="ev ev-answer">${esc(e.text)} ${t}</div>`;
    case "hint":
      return `<div class="ev ev-hint">${t} 💡 Подсказка: ${esc(e.text)}</div>`;
    case "note":
      return `<div class="ev ev-hint">${t} 📝 ${esc(e.text)}</div>`;
    case "done":
      return `<div class="ev ev-done">✔ Решено: ${esc(e.text)}</div>`;
    case "failed":
      return `<div class="ev ev-failed">✘ Не решено: ${esc(e.text)}</div>`;
    case "step": {
      const img = d.image ? `<img src="/files/${S.ticketId}/${encodeURIComponent(d.image)}?t=${encodeURIComponent(APP.token)}" alt="скриншот" loading="lazy">` : "";
      return `<div class="ev ev-step">
        <div class="head"><b>Шаг ${esc(d.step)}</b>${d.plan_step ? `<span class="pill">пункт плана ${esc(d.plan_step)}</span>` : ""}
          ${d.risky ? `<span class="pill risky">риск</span>` : ""}<span class="spacer"></span>${t}</div>
        ${d.observation ? `<div class="observation">👁 ${esc(d.observation)}</div>` : ""}
        ${e.text ? `<div>${esc(e.text)}</div>` : ""}
        ${["run", "wait"].includes(d.action) ? "" : `<div class="action">${esc(d.description)}</div>`}
        ${img}
      </div>`;
    }
    case "command": {
      const status = d.status === "done"
        ? `<span class="${d.exit_code ? "exit-bad" : "exit-ok"}">код ${esc(d.exit_code ?? "?")}</span>`
        : d.status === "running" ? `<span>ещё выполняется…</span>` : `<span class="exit-bad">консоль не ответила</span>`;
      const out = (d.output || "").trim();
      const long = out.split("\n").length > 14;
      return `<div class="ev term">
        <div class="cmd"><span class="ps">PS&gt;</span><code>${esc(d.command || e.text)}</code>${t}</div>
        ${out ? `<pre>${esc(out)}</pre>` : ""}
        <div class="foot">${status}${long ? `<span class="spacer"></span><button data-expand>развернуть</button>` : ""}</div>
      </div>`;
    }
    default:
      return `<div class="ev ev-line">${t}<span>${esc(e.text)}</span></div>`;
  }
}

/* ------------------------------------------------------------------ настройки */

async function viewSettings() {
  const { settings: s, presets } = await api("/api/settings");
  const preset = presets[s.provider] || {};
  $("#main").innerHTML = `
    <h1 style="margin-bottom:16px">Настройки</h1>
    <form id="settings-form" class="settings form">
      <section class="card form">
        <h2>Модель (Qwen через облачный API)</h2>
        <label class="field">Провайдер
          <select name="provider">${Object.entries(presets).map(([k, p]) =>
            `<option value="${k}" ${k === s.provider ? "selected" : ""}>${esc(p.title)}</option>`).join("")}</select>
        </label>
        <label class="field">Адрес API<input name="base_url" value="${esc(s.base_url)}"></label>
        <label class="field">API-ключ
          <span class="hint" id="key-hint">${s.api_key_set ? `Ключ задан (${esc(s.api_key_hint)}). Оставьте поле пустым, чтобы не менять.` : "Ключ не задан."}
            ${preset.key_url ? `<a href="${esc(preset.key_url)}" target="_blank" rel="noopener">Получить ключ</a>` : ""}</span>
          <input name="api_key" type="password" autocomplete="off" placeholder="${s.api_key_set ? "••••••••" : "sk-..."}">
        </label>
        <label class="field">Модель
          <span class="hint">Нужна модель, которая понимает картинки (Qwen-VL), если включены скриншоты. Для OpenRouter бесплатные модели оканчиваются на :free.</span>
          <div class="actions" style="flex-wrap:nowrap">
            <input name="model" list="models" value="${esc(s.model)}">
            <button type="button" class="btn" id="load-models">Загрузить список</button>
          </div>
          <datalist id="models"></datalist>
        </label>
        <div class="form-row">
          <label class="check"><input type="checkbox" name="vision" ${s.vision ? "checked" : ""}> Скриншоты и мышь (модель видит экран)</label>
          <label class="check"><input type="checkbox" name="json_mode" ${s.json_mode ? "checked" : ""}> Строгий JSON-режим API</label>
        </div>
        <label class="field">Формат координат
          <select name="coords">
            <option value="pixels" ${s.coords === "pixels" ? "selected" : ""}>Пиксели (Qwen2.5-VL, qwen-vl-max)</option>
            <option value="norm1000" ${s.coords === "norm1000" ? "selected" : ""}>0..1000 (Qwen3-VL)</option>
          </select>
        </label>
        <div class="actions">
          <button type="button" class="btn" id="check-model">Проверить модель</button>
          <span class="muted" style="font-size:12px">сначала сохраните изменения</span>
        </div>
        <pre class="report hidden" id="check-report"></pre>
      </section>

      <section class="card form">
        <h2>Агент</h2>
        <label class="field">Что подтверждать вручную
          <select name="confirm">
            <option value="risky" ${s.confirm === "risky" ? "selected" : ""}>Только рискованное: установка, удаление, реестр, админ-права (рекомендуется)</option>
            <option value="all" ${s.confirm === "all" ? "selected" : ""}>Каждое действие</option>
            <option value="none" ${s.confirm === "none" ? "selected" : ""}>Ничего (полностью автономно)</option>
          </select>
        </label>
        <div class="form-row">
          <label class="field">Язык ответов ИИ<input name="language" value="${esc(s.language)}"></label>
          <label class="field">Максимум шагов<input name="max_steps" type="number" min="5" max="500" value="${s.max_steps}"></label>
        </div>
      </section>

      <section class="card form">
        <h2>AnyDesk</h2>
        <label class="field">Путь к AnyDesk.exe
          <span class="hint">Пусто — найти автоматически; если AnyDesk не установлен, программа скачает его с anydesk.com.</span>
          <input name="anydesk_path" value="${esc(s.anydesk_path)}" placeholder="C:\\Program Files (x86)\\AnyDesk\\AnyDesk.exe">
        </label>
        <div class="form-row">
          <label class="field">Ждать подключения, сек<input name="connect_timeout" type="number" min="10" value="${s.connect_timeout}"></label>
          <label class="field">Заголовок окна сеанса<input name="window_title" value="${esc(s.window_title)}" placeholder="авто по ID"></label>
        </div>
      </section>

      <section class="card form">
        <h2>Программа и обновления</h2>
        <label class="check"><input type="checkbox" name="auto_update" ${s.auto_update ? "checked" : ""}> Проверять обновления на GitHub при запуске</label>
        <label class="field">Порт веб-интерфейса<input name="port" type="number" value="${s.port}"></label>
        <div class="actions">
          <span>Версия <b>${esc(APP.version)}</b></span>
          <button type="button" class="btn" id="upd-check">Проверить обновления</button>
          <button type="button" class="btn green hidden" id="upd-install"></button>
        </div>
        <p class="muted hidden" id="upd-text" style="margin:0"></p>
      </section>

      <div class="actions"><button class="btn primary">Сохранить</button></div>
    </form>`;

  const form = $("#settings-form");
  form.provider.addEventListener("change", () => {
    const p = presets[form.provider.value];
    if (p.base_url) form.base_url.value = p.base_url;
    if (p.model) form.model.value = p.model;
    form.coords.value = p.coords;
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(async () => {
      const data = Object.fromEntries(new FormData(form));
      for (const name of ["vision", "json_mode", "auto_update"]) data[name] = form[name].checked;
      await api("/api/settings", { method: "POST", body: data });
      toast("Настройки сохранены");
      viewSettings();
    });
  });
  $("#load-models").addEventListener("click", () => guarded(async () => {
    const models = await api("/api/settings/models", { method: "POST" });
    $("#models").innerHTML = models.map((m) =>
      `<option value="${esc(m.id)}">${m.vision ? "👁 " : ""}${m.free ? "бесплатно" : ""}</option>`).join("");
    toast(`Моделей: ${models.length}. Начните вводить «qwen» в поле модели`);
  }));
  $("#check-model").addEventListener("click", () => guarded(async () => {
    const rep = $("#check-report");
    rep.classList.remove("hidden");
    rep.textContent = "Проверяю… (до минуты)";
    const r = await api("/api/settings/check", { method: "POST" });
    rep.textContent = r.report.join("\n");
  }));
  $("#upd-check").addEventListener("click", () => guarded(async () => {
    const st = await api("/api/update/check", { method: "POST" });
    showUpdate(st);
  }));
  $("#upd-install").addEventListener("click", () => guarded(async () => {
    const r = await api("/api/update/install", { method: "POST" });
    toast(r.message);
    setTimeout(waitForRestart, 3000);
  }));
  if (S.state.update) showUpdate(S.state);
}

function showUpdate(st) {
  const text = $("#upd-text");
  const btn = $("#upd-install");
  if (!text) return;
  text.classList.remove("hidden");
  if (st.update_error) text.textContent = "Ошибка проверки: " + st.update_error;
  else if (st.update) {
    text.textContent = `Доступна версия ${st.update.version}. ${st.update.notes || ""}`;
    if (st.frozen) { btn.textContent = `Обновить до ${st.update.version}`; btn.classList.remove("hidden"); }
    else text.textContent += " (из исходников обновляйтесь через git pull)";
  } else text.textContent = "У вас последняя версия.";
}

async function waitForRestart() {
  try {
    await fetch("/", { cache: "no-store" });
    location.reload();
  } catch { setTimeout(waitForRestart, 2000); }
}

/* ------------------------------------------------------------------ запуск */

document.addEventListener("click", (e) => {
  const img = e.target.closest(".ev-step img");
  if (img) {
    $("#lightbox img").src = img.src;
    $("#lightbox").classList.remove("hidden");
  } else if (e.target.closest("#lightbox")) {
    $("#lightbox").classList.add("hidden");
  }
  const exp = e.target.closest("[data-expand]");
  if (exp) {
    const pre = exp.closest(".term").querySelector("pre");
    pre.classList.toggle("expanded");
    exp.textContent = pre.classList.contains("expanded") ? "свернуть" : "развернуть";
  }
});
$("#update-badge").addEventListener("click", () => { location.hash = "#/settings"; });
let searchTimer;
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadTickets, 250); });
$("#status-filter").addEventListener("change", loadTickets);
window.addEventListener("hashchange", route);

(async function init() {
  $("#version").textContent = "v" + APP.version;
  await guarded(loadState);
  await guarded(loadTickets);
  route();
  setInterval(() => { if (S.ticket) refreshTicket().catch(() => {}); }, 1500);
  setInterval(() => { loadTickets().catch(() => {}); loadState().catch(() => {}); }, 4000);
})();
