"use strict";

/* ================================================================== утилиты */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const time = (ts) => (ts || "").slice(11, 16);
const date = (ts) => (ts || "").replace("T", " ").slice(0, 16);

const STATUS = {
  new: "новый", running: "в работе", waiting: "ждёт ответа", paused: "пауза",
  solved: "решён", failed: "не решён", stopped: "остановлен",
};
const KIND = { solution: "рецепт", error_fix: "ошибка → решение", pitfall: "не делать", manual: "от техника" };
const pill = (status) => `<span class="pill ${esc(status)}">${esc(STATUS[status] || status)}</span>`;
const kindPill = (k) => `<span class="pill k-${esc(k)}">${esc(KIND[k] || k)}</span>`;

const ICON = {
  pause: `<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><rect x="3" y="2" width="3.5" height="12" rx="1"/><rect x="9.5" y="2" width="3.5" height="12" rx="1"/></svg>`,
  play: `<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><path d="M4 2.5v11a.5.5 0 0 0 .77.42l8.5-5.5a.5.5 0 0 0 0-.84l-8.5-5.5A.5.5 0 0 0 4 2.5z"/></svg>`,
  stop: `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor"><rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/></svg>`,
};
const ADM_ICON = { tip: "💡", info: "ℹ️", warn: "⚠️", danger: "🔥", note: "📝" };
function adm(type, title, body) {
  return `<div class="adm ${type}"><div class="adm-title">${ADM_ICON[type] || ""} ${esc(title)}</div>${body}</div>`;
}
function codeBlock(text, { title = "", prompt = false, foot = "" } = {}) {
  const long = String(text).split("\n").length > 14;
  return `<div class="code">
    <div class="bar"><span class="title">${esc(title)}</span><span class="spacer"></span>${foot}
      ${long ? `<button data-expand>развернуть</button>` : ""}<button data-copy>копировать</button></div>
    <pre>${prompt ? `<span class="prompt">PS&gt; </span>` : ""}${esc(text)}</pre></div>`;
}

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(path, {
    method,
    headers: { "X-Token": APP.token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) { location.reload(); throw new Error("Сессия устарела"); }
  const data = resp.headers.get("Content-Type")?.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) throw Object.assign(new Error((data && data.error) || `Ошибка ${resp.status}`), { status: resp.status });
  return data;
}

// Меняет разметку, только если она другая: опрос раз в 1,5–4 с иначе сбрасывает выделение
// текста и «съедает» клик, если кнопку заменили между нажатием и отпусканием.
function setHTML(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
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

function storeGet(key, def) { try { return localStorage.getItem(key) ?? def; } catch { return def; } }
function storeSet(key, val) { try { localStorage.setItem(key, val); } catch { /* приватный режим */ } }

/* ================================================================== состояние */

const S = {
  tickets: [],
  lessons: [],
  state: { counts: {}, model: {}, knowledge: null },
  route: "",
  ticketId: null,
  ticket: null,
  lastEventId: 0,
  prompts: {},
  planStep: null,
  pendingId: null,
};

/* ================================================================== навбар */

async function loadState() {
  S.state = await api("/api/state");
  renderNavbar();
  if (S.route) watchAsk().catch(() => {}); // до первого route() не знаем, какой тикет открыт
}

/* ---------------------------------------------------------------- сигнал «ИИ ждёт ответа» */

// Обычно поверх браузера окно AnyDesk: без звука и уведомления вопрос может висеть час.
// Звук и уведомления браузер разрешает только после действия пользователя — включаем по клику.
let audioCtx = null;
function unlockAlerts(askPermission = false) {
  try {
    audioCtx ??= new AudioContext();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch { /* нет WebAudio */ }
  try {
    if (askPermission && window.Notification?.permission === "default") Notification.requestPermission();
  } catch { /* нет уведомлений */ }
}

function beep() {
  if (audioCtx?.state !== "running") return;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.frequency.value = 880;
  gain.gain.value = 0.15;
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 0.15);
}

const seenAsks = new Set();
// Показанное уведомление о вопросе: { tid, n }. Новое с тем же tag браузер подменяет молча
// (без звука и всплывания), поэтому старое закрываем сами и просим renotify.
let askNote = null;
function closeAskNote(tid) {
  if (!askNote || (tid !== undefined && askNote.tid !== tid)) return;
  try { askNote.n.close(); } catch { /* уже закрыто */ }
  askNote = null;
}

function notifyAsk(tid, ask) {
  const key = `${tid}:${ask.id}`;
  if (seenAsks.has(key)) return; // каждый вопрос — один сигнал
  seenAsks.add(key);
  closeAskNote(); // прежний вопрос уже снят: его уведомление устарело
  if (document.hasFocus() && S.ticketId === tid) return; // техник и так смотрит на вопрос
  try {
    beep();
    if (window.Notification?.permission !== "granted") return;
    const n = new Notification(`Тикет #${tid}: ${ASK_LABEL[ask.kind] || "Вопрос"}`,
      { body: String(ask.text || "").slice(0, 200), tag: "aih-ask", renotify: true, requireInteraction: true });
    askNote = { tid, n };
    n.onclick = () => { window.focus(); location.hash = `#/t/${tid}`; closeAskNote(tid); };
  } catch { /* звук или уведомления недоступны */ }
}

// Вопрос активного сеанса: из /api/state (active_ask), а если сервер его не отдаёт — из тикета.
// Открытый тикет сигналит (и снимает уведомление) сам из renderPending.
async function watchAsk() {
  const st = S.state;
  if (!st.active_ticket) return closeAskNote(); // сеанс кончился — вопроса нет
  if (st.active_ticket === S.ticketId) return;
  let ask = st.active_ask;
  if (ask === undefined && st.active_status === "waiting")
    ask = (await api(`/api/tickets/${st.active_ticket}`)).session?.pending;
  if (ask) notifyAsk(st.active_ticket, ask);
  else closeAskNote(); // на вопрос ответили (в этой или другой вкладке)
}

function renderNavbar() {
  const st = S.state;
  $("#version").textContent = "v" + (st.version || APP.version);
  const c = st.counts || {};
  const open = (c.running || 0) + (c.waiting || 0) + (c.paused || 0) + (c.new || 0);
  $("#tab-count").textContent = open || "";
  $("#kb-count").textContent = st.knowledge?.lessons || "";
  document.querySelectorAll(".tabs a").forEach((a) => {
    const tab = a.dataset.tab;
    const active = (tab === "tickets" && ["tickets", "new", "ticket"].includes(S.route)) || tab === S.route;
    a.classList.toggle("active", active);
  });

  // живой статус агента
  const w = $("#agent-widget");
  const line = $("#run-line");
  const waiting = st.active_status === "waiting";
  let html;
  if (st.active_ticket) {
    const paused = st.active_status === "paused";
    html = `<div class="session ${waiting ? "waiting" : ""}">
      <span class="dot ${waiting || paused ? "wait" : "busy"}"></span>
      <a href="#/t/${st.active_ticket}">Тикет #${st.active_ticket}</a>
      <span class="muted status-text">${esc(STATUS[st.active_status] || "в работе")}</span>
      ${paused ? `<button class="mini" data-nav-ctl="resume" title="Продолжить">${ICON.play}</button>`
               : `<button class="mini" data-nav-ctl="pause" title="Пауза">${ICON.pause}</button>`}
      <button class="mini" data-nav-ctl="stop" title="Стоп">${ICON.stop}</button></div>`;
    line.className = "run-line on" + (waiting || paused ? " wait" : "");
  } else {
    html = `<span class="idle"><span class="dot"></span>Агент свободен</span>`;
    line.className = "run-line";
  }
  // Номер тикета есть в разметке: если она не изменилась, старые обработчики верны.
  if (setHTML(w, html)) w.querySelectorAll("[data-nav-ctl]").forEach((b) =>
    b.addEventListener("click", () => control(b.dataset.navCtl, st.active_ticket)));
  // Видно на вкладке, даже когда браузер за окном AnyDesk.
  document.title = st.active_ticket && waiting ? `❓ Ждёт ответа · тикет #${st.active_ticket}` : "AnyDesk Helper";

  // модель
  const m = st.model || {};
  const name = (m.name || "модель не выбрана").split("/").pop();
  setHTML($("#model-chip"), `<span class="dot ${m.key ? "ok" : "bad"}"></span>🧠 ${esc(name)}${m.key ? "" : " · нет ключа"}`);
  $("#model-chip").title = m.key ? `Модель: ${m.name}` : "Не задан API-ключ — откройте настройки";

  const up = $("#update-chip");
  if (st.update) {
    up.textContent = st.installing ? "⏳ Обновление…" : `⬆ v${st.update.version}`;
    up.title = st.installing ? "Скачиваю обновление… программа перезапустится" : "Доступно обновление";
    up.classList.remove("hidden");
  } else up.classList.add("hidden");
  $("#theme-btn").textContent = document.documentElement.dataset.theme === "light" ? "🌙" : "☀️";
}

$("#theme-btn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  storeSet("theme", next);
  renderNavbar();
});

/* ================================================================== список тикетов */

async function loadTickets() {
  const q = encodeURIComponent($("#search").value.trim());
  const st = encodeURIComponent($("#status-filter").value);
  S.tickets = await api(`/api/tickets?q=${q}&status=${st}`);
  renderList();
}

function renderList() {
  const list = $("#ticket-list");
  if (!S.tickets.length) {
    setHTML(list, `<p class="muted small" style="padding:8px">Тикетов пока нет</p>`);
    return;
  }
  setHTML(list, S.tickets.map((t) => `
    <a class="ticket-item ${t.id === S.ticketId ? "active" : ""}" href="#/t/${t.id}">
      <div class="row"><span class="title">${esc(t.title)}</span>${pill(t.status)}</div>
      <div class="meta">#${t.id} · ${esc(t.anydesk_id || "без AnyDesk")} · ${esc(date(t.updated_at))}</div>
    </a>`).join(""));
}

/* ================================================================== роутинг */

function route() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/t\/(\d+)/);
  S.ticketId = m ? Number(m[1]) : null;
  S.ticket = null;
  S.route = m ? "ticket" : (hash.match(/^#\/(\w+)/)?.[1] || "home");
  $("#layout").classList.toggle("no-sidebar", !["tickets", "new", "ticket"].includes(S.route));
  $("#main").scrollTop = 0;
  renderNavbar();
  renderList();
  if (m) return viewTicket(S.ticketId);
  if (S.route === "new") return viewNew();
  if (S.route === "tickets") return viewTickets();
  if (S.route === "kb") return viewKnowledge();
  if (S.route === "settings") return viewSettings();
  return viewHome();
}

/* ================================================================== главная */

function viewHome() {
  const st = S.state;
  const c = st.counts || {};
  const k = st.knowledge || { lessons: 0, wins: 0 };
  const noKey = !st.model?.key;
  $("#main").innerHTML = `<div class="container">
    <section class="hero">
      <h1>AnyDesk <span class="accent">Helper</span></h1>
      <p class="tagline">ИИ-техподдержка, которая сама подключается по AnyDesk, задаёт вопросы,
        чинит через PowerShell и учится на своих ошибках.</p>
      <div class="actions">
        <a class="btn primary lg" href="#/new">+ Новый тикет</a>
        <a class="btn lg" href="#/kb">📚 База опыта</a>
      </div>
    </section>
    ${noKey ? adm("warn", "Модель не настроена", `<p>Укажите API-ключ провайдера (OpenRouter или DashScope) в
      <a href="#/settings">настройках</a> — без него агент не сможет думать.</p>`) : ""}
    <div class="stats" style="margin-top:20px">
      <div class="stat"><div class="num">${c.solved || 0}</div><div class="lbl">тикетов решено</div></div>
      <div class="stat"><div class="num">${(c.running || 0) + (c.waiting || 0) + (c.new || 0)}</div><div class="lbl">открыто сейчас</div></div>
      <div class="stat"><div class="num">${k.lessons}</div><div class="lbl">уроков в базе опыта</div></div>
      <div class="stat"><div class="num">${k.wins}</div><div class="lbl">раз опыт помог решить</div></div>
    </div>
    <h2 class="section-title">Как это работает</h2>
    <div class="features">
      <div class="feature"><div class="ico">🎫</div><h3>Тикет</h3><p>Опишите проблему и укажите ID AnyDesk пользователя.</p></div>
      <div class="feature"><div class="ico">💬</div><h3>Вопросы и план</h3><p>Qwen изучает систему, спрашивает недостающее и пишет план — вы его одобряете.</p></div>
      <div class="feature"><div class="ico">⌨️</div><h3>Консоль</h3><p>Команды идут в PowerShell пользователя, вывод возвращается точным текстом.</p></div>
      <div class="feature"><div class="ico">🧠</div><h3>Опыт</h3><p>После каждого тикета агент записывает уроки и в следующий раз решает быстрее.</p></div>
    </div>
    ${adm("tip", "Перед первым сеансом", `<p>В AnyDesk на этом ПК включите <b>Настройки → Ввод → «Передавать комбинации клавиш»</b>
      и не отключайте синхронизацию буфера обмена — через неё идут команды и их вывод.</p>`)}
  </div>`;
}

function viewTickets() {
  $("#main").innerHTML = `<div class="empty">
    <h2>Выберите тикет слева</h2>
    <p>или создайте новый — агент подключится, задаст вопросы и решит задачу.</p>
    <p><a class="btn primary" href="#/new">+ Новый тикет</a></p></div>`;
}

/* ================================================================== новый тикет */

function viewNew() {
  $("#main").innerHTML = `<div class="narrow">
    <div class="page-head"><div><h1>Новый тикет</h1><p>Чем подробнее описание, тем меньше вопросов задаст ИИ.</p></div></div>
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
        <span class="hint">Что случилось, что уже пробовали, пожелания пользователя (версия, язык, лицензия…)</span>
        <textarea name="description" rows="6" placeholder="Бухгалтеру нужны Word и Excel. Лицензия Microsoft 365 для бизнеса, учётка есть. Windows 10."></textarea>
      </label>
      <div class="actions">
        <button class="btn primary" type="submit">Создать тикет</button>
        <a class="btn ghost" href="#/tickets">Отмена</a>
      </div>
    </form></div>`;
  $("#new-form").addEventListener("submit", (e) => {
    e.preventDefault();
    // Двойной клик или двойной Enter не создаст второй тикет: неактивная кнопка блокирует и Enter.
    const btn = e.submitter || e.target.querySelector("[type=submit]");
    btn.disabled = true;
    guarded(async () => {
      const t = await api("/api/tickets", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
      await loadTickets();
      location.hash = `#/t/${t.id}`;
    }).finally(() => { btn.disabled = false; });
  });
}

/* ================================================================== тикет */

async function viewTicket(id) {
  S.lastEventId = 0;
  S.prompts = {};
  S.planStep = null;
  S.pendingId = null;
  $("#main").innerHTML = `<p class="muted">Загрузка…</p>`;
  let t;
  try {
    t = await api(`/api/tickets/${id}`);
  } catch (e) {
    if (S.ticketId !== id) return;
    $("#main").innerHTML = `<div class="empty"><h2>Тикет #${id} не открылся</h2>
      <p>${e.status === 404 ? "Такого тикета нет — возможно, он удалён." : esc(e.message)}</p>
      <p><a class="btn" href="#/tickets">К списку тикетов</a></p></div>`;
    return;
  }
  if (S.ticketId !== id) return;
  S.ticket = t;
  $("#main").innerHTML = `<div class="container">
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
          <div class="actions" style="margin-top:8px"><button class="btn sm" id="hint-send">Отправить</button></div>
        </div>
        <div class="card"><h3>Промт для ИИ</h3><div id="prompts" class="muted small">Появится после запуска</div></div>
      </div>
    </div></div>`;
  $("#hint-send").addEventListener("click", sendHint);
  renderTicketMeta();
  await pollEvents();
}

function renderTicketMeta() {
  const t = S.ticket;
  const sess = t.session || {};
  setHTML($("#t-info"), `
    <h1>${esc(t.title)}</h1>
    <div class="meta">
      <span class="mono">#${t.id}</span> ${pill(t.status)}
      <span>AnyDesk <b class="mono">${esc(t.anydesk_id || "—")}</b></span>
      <span>создан ${esc(date(t.created_at))}</span>
    </div>
    ${t.description ? `<p class="description">${esc(t.description)}</p>` : ""}
    ${t.summary ? `<div style="margin-top:10px">${adm(t.status === "solved" ? "tip" : "note", "Итог", `<p>${esc(t.summary)}</p>`)}</div>` : ""}`);

  const acts = [];
  if (sess.active) {
    acts.push(sess.paused
      ? `<button class="btn" data-ctl="resume">${ICON.play} Продолжить</button>`
      : `<button class="btn" data-ctl="pause">${ICON.pause} Пауза</button>`);
    acts.push(`<button class="btn danger" data-ctl="stop">${ICON.stop} Стоп</button>`);
  } else {
    acts.push(`<button class="btn ghost" id="edit-btn">Изменить</button>`);
    acts.push(`<button class="btn ghost danger" id="delete-btn">Удалить</button>`);
  }
  acts.push(`<a class="btn ghost" href="/api/tickets/${t.id}/export?t=${encodeURIComponent(APP.token)}">⬇ Отчёт</a>`);
  if (setHTML($("#t-actions"), acts.join(""))) { // id тикета есть в ссылке «Отчёт»
    $("#t-actions").querySelectorAll("[data-ctl]").forEach((b) =>
      b.addEventListener("click", () => control(b.dataset.ctl, t.id)));
    $("#edit-btn")?.addEventListener("click", showEdit);
    $("#delete-btn")?.addEventListener("click", deleteTicket);
  }

  renderStartCard();
  renderPlan();
  renderPending();
  $("#hint-title").textContent = sess.active ? "Подсказка агенту" : "Заметка к тикету";
}

function renderStartCard() {
  const t = S.ticket;
  const card = $("#start-card");
  const busy = S.state.active_ticket && S.state.active_ticket !== t.id;
  const installing = !!S.state.installing; // после обновления процесс перезапустится — сеанс не начать
  // Перерисовываем только при смене состояния — иначе сотрём введённый пароль.
  const key = `${t.id}|${!!t.session?.active}|${busy}|${t.status === "new"}|${installing}`;
  if (card.dataset.key === key) return;
  card.dataset.key = key;
  if (t.session?.active) {
    card.innerHTML = `<h3>Сеанс идёт</h3>
      <p class="muted small" style="margin:0">Не трогайте мышь и клавиатуру этого ПК: агент управляет окном AnyDesk.
      Аварийная остановка — мышь в левый верхний угол экрана.</p>`;
    return;
  }
  const canResume = t.status !== "new" && t.status !== "solved" && (t.plan || []).length > 0;
  card.innerHTML = `
    <h3>${t.status === "new" ? "Запуск агента" : canResume ? "Продолжить работу" : "Запустить заново"}</h3>
    <div class="form">
      ${APP.dev ? `
        <label class="field">Режим
          <select id="st-mode"><option value="anydesk">AnyDesk</option><option value="local">Этот ПК (тест)</option></select>
        </label>
        <label class="check"><input type="checkbox" id="st-dry"> Без нажатий (dry-run)</label>` : ""}
      ${canResume ? `<label class="check"><input type="checkbox" id="st-resume" checked> Продолжить по сохранённому плану
        (${t.plan.length} пунктов, ответы и история сохранятся)</label>` : ""}
      <label class="check"><input type="checkbox" id="st-connect" checked> Подключиться к ${esc(t.anydesk_id || "?")} автоматически</label>
      <label class="field">Пароль неконтролируемого доступа
        <span class="hint">Необязательно. Без пароля пользователь сам примет подключение. Не сохраняется.</span>
        <input type="password" id="st-password" autocomplete="off">
      </label>
      <button class="btn primary" id="st-start" ${busy || installing ? "disabled" : ""}>▶ Запустить агента</button>
      ${installing ? `<span class="muted small">Скачиваю обновление… программа перезапустится</span>`
        : busy ? `<span class="muted small">Сейчас идёт сеанс по тикету #${S.state.active_ticket}</span>` : ""}
    </div>`;
  $("#st-start").addEventListener("click", () => guarded(async () => {
    unlockAlerts(true); // клик — жест пользователя: можно спросить про уведомления и включить звук
    await api(`/api/tickets/${t.id}/start`, {
      method: "POST",
      body: {
        password: $("#st-password").value,
        connect: $("#st-connect").checked,
        mode: $("#st-mode")?.value || "anydesk",
        dry_run: $("#st-dry")?.checked || false,
        resume: $("#st-resume")?.checked || false,
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
  setHTML($("#plan"), plan.length
    ? `<ol class="plan-list">${plan.map((p, i) => {
        const n = i + 1;
        const cls = S.planStep === n ? "current" : (S.planStep && n < S.planStep ? "past" : "");
        return `<li class="${cls}">${esc(p)}</li>`;
      }).join("")}</ol>`
    : `<p class="muted small" style="margin:0">ИИ составит план после запуска</p>`);
}

/* ---------------------------------------------------------------- вопрос ИИ */

const ASK_LABEL = {
  question: "ИИ спрашивает", plan: "Подтвердите план", confirm: "Подтвердите действие",
  // error — и сбой модели, и окно AnyDesk не вышло на передний план: оба вопроса «повторить?»
  done: "Задача решена?", fail: "ИИ не справляется", error: "Ошибка — повторить?", pick_window: "Нужна помощь",
};

function renderPending() {
  const box = $("#pending");
  const p = S.ticket.session?.pending;
  if (!p) { box.innerHTML = ""; S.pendingId = null; closeAskNote(S.ticketId); return; }
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
      body = `${d.command ? codeBlock(d.command, { title: "PowerShell", prompt: true }) : ""}
        ${d.risky ? `<span><span class="pill risky">ИИ пометил как рискованное</span></span>` : ""}
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
  const type = p.kind === "error" || p.kind === "fail" ? "danger" : p.kind === "done" ? "tip" : "warn";
  // Техник печатает в другом поле (подсказка, поиск) — фокус не забираем, иначе следующие буквы
  // и Enter уйдут в ответ (например, «Отклонить с подсказкой»). Считаем до замены разметки.
  const a = document.activeElement;
  const typing = !!a && !box.contains(a) &&
    a.matches("textarea, select, input:not([type=checkbox]):not([type=radio])");
  box.innerHTML = `<div class="adm ${type} pending">
      <div class="adm-title">${ADM_ICON[type]} ${esc(ASK_LABEL[p.kind] || "Вопрос")}</div>
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
  const yes = box.querySelector('[data-btn="yes"]');
  // Фокус на «да» ставим мы, а не техник: удержанный Enter (автоповтор) или дребезг в первые
  // 300 мс не должен нажать её — одобряет только новое, отдельное нажатие.
  let armedAt = 0;
  const focusYes = () => { yes.focus(); armedAt = Date.now() + 300; };
  yes?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.repeat || Date.now() < armedAt)) e.preventDefault();
  });
  // Без поля ввода (error, pick_window) фокус на зелёной кнопке: Enter/пробел нажмут её.
  if (!typing) { if (input) input.focus(); else if (yes) focusYes(); }
  if (input) {
    const enter = input.tagName === "INPUT" ? "Enter" : "Ctrl+Enter";
    if (yes) input.title = `Пустое поле: ${enter} — перейти к «${yes.textContent.trim()}», ещё Enter — нажать`;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && ((!e.shiftKey && input.tagName === "INPUT") || e.ctrlKey)) {
        e.preventDefault();
        // Пустое поле: только переводим фокус на «да» — одобряет второй, осознанный Enter.
        if (!input.value.trim() && yes) focusYes();
        else box.querySelector("[data-send]")?.click();
      }
    });
  }
  if (!typing) box.scrollIntoView({ block: "nearest" });
  notifyAsk(S.ticketId, p);
}

async function answer(askId, value, button) {
  await guarded(async () => {
    await api(`/api/tickets/${S.ticketId}/answer`, { method: "POST", body: { ask_id: askId, value, button } });
    $("#pending").innerHTML = "";
    await refreshTicket();
    loadTickets();
  });
}

async function control(cmd, ticketId) {
  if (cmd === "stop" && !confirm("Остановить агента?")) return;
  await guarded(async () => {
    await api(`/api/tickets/${ticketId}/control`, { method: "POST", body: { cmd } });
    await loadState();
    if (S.ticketId === ticketId) await refreshTicket();
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
    location.hash = "#/tickets";
  });
}

/* ---------------------------------------------------------------- лента */

async function refreshTicket() {
  if (!S.ticketId) return;
  const t = await api(`/api/tickets/${S.ticketId}`);
  if (t.id !== S.ticketId || !$("#t-info")) return;
  S.ticket = t;
  renderTicketMeta();
  await pollEvents();
}

async function pollEvents() {
  const id = S.ticketId;
  const after = S.lastEventId;
  const events = await api(`/api/tickets/${id}/events?after=${after}`);
  // Опросы идут параллельно (таймер, подсказка, ответ): если другой уже дорисовал события
  // или тикет открыли заново, этот ответ устарел — иначе история задвоится.
  if (id !== S.ticketId || after !== S.lastEventId || !$("#timeline")) return;
  const tl = $("#timeline");
  const main = $("#main");
  const nearBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 200;
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
    if (nearBottom) main.scrollTop = main.scrollHeight;
  }
}

function renderPrompts() {
  $("#prompts").innerHTML = Object.entries(S.prompts).map(([title, d]) => `
    <details style="margin-top:6px"><summary>${esc(title)}</summary>
      <p class="muted small" style="margin:6px 0 2px">Системный промт (инструкция и формат ответа):</p><pre>${esc(d.system)}</pre>
      <p class="muted small" style="margin:6px 0 2px">Сообщение с данными тикета:</p><pre>${esc(d.user)}</pre>
    </details>`).join("");
}

function lessonList(lessons) {
  return `<ul class="lesson-links">${(lessons || []).map((l) =>
    `<li>${kindPill(l.kind)} <a href="#/kb?id=${l.id}">${esc(l.title)}</a></li>`).join("")}</ul>`;
}

// Статусы CommandResult из bridge.py (кроме done — там код возврата).
const CMD_FOOT = {
  running: "выполняется…",
  busy: "не отправлена: предыдущая команда ещё выполняется",
  refused: "отклонена: консоль на компьютере техника",
  no_ack: "консоль не ответила",
};
const CMD_WAIT = new Set(["running", "busy"]); // не ошибка: результат будет или команду повторят

function renderEvent(e) {
  const d = e.data || {};
  const t = `<span class="time">${time(e.ts)}</span>`;
  switch (e.kind) {
    case "status":
    case "info":
      return `<div class="ev-line">${t}<span>${esc(e.text)}</span></div>`;
    case "warning":
      return adm("warn", "Внимание", `<p>${esc(e.text)}</p>`);
    case "error":
      return adm("danger", "Ошибка", `<p>${esc(e.text)}</p>
        ${d.traceback ? `<details><summary>Подробности</summary><pre>${esc(d.traceback)}</pre></details>` : ""}`);
    case "plan":
      return adm("info", "План готов", `<p>${esc(e.text)}</p>`);
    case "ask":
      return `<div class="ev-line">${t}<span>❓ <b>${esc(ASK_LABEL[d.ask_kind] || "Вопрос")}:</b> ${esc(d.ask_kind === "confirm" && d.command ? d.command : e.text)}</span></div>`;
    case "answer":
      return `<div class="ev-answer">${esc(e.text)} ${t}</div>`;
    case "hint":
      return adm("note", "Подсказка техника", `<p>${esc(e.text)}</p>`);
    case "note":
      return adm("note", "Заметка", `<p>${esc(e.text)}</p>`);
    case "experience":
      return adm("info", "Опыт из базы", `<p>${esc(e.text)}</p>${lessonList(d.lessons)}`);
    case "learned":
      return adm("tip", "Агент научился", `<p>${esc(e.text)}</p>${lessonList(d.lessons)}`);
    case "done":
      return adm("tip", "Решено", `<p>${esc(e.text)}</p>`);
    case "failed":
      return adm("danger", "Не решено", `<p>${esc(e.text)}</p>`);
    case "step": {
      const img = d.image ? `<img src="/files/${S.ticketId}/${encodeURIComponent(d.image)}?t=${encodeURIComponent(APP.token)}" alt="скриншот" loading="lazy">` : "";
      return `<div class="ev-step">
        <div class="head"><b>Шаг ${esc(d.step)}</b>${d.plan_step ? `<span class="pill">пункт ${esc(d.plan_step)}</span>` : ""}
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
        : `<span class="${CMD_WAIT.has(d.status) ? "" : "exit-bad"}">${esc(CMD_FOOT[d.status] || CMD_FOOT.no_ack)}</span>`;
      const out = (d.output || "").trim();
      return codeBlock(`${d.command || e.text}${out ? "\n\n" + out : ""}`,
        { title: `PowerShell · ${time(e.ts)}`, prompt: true, foot: status });
    }
    default:
      return `<div class="ev-line">${t}<span>${esc(e.text)}</span></div>`;
  }
}

/* ================================================================== база опыта */

async function viewKnowledge() {
  const focus = Number((location.hash.match(/id=(\d+)/) || [])[1] || 0);
  $("#main").innerHTML = `<div class="narrow">
    <div class="page-head">
      <div style="flex:1"><h1>База опыта</h1>
        <p>Уроки, которые агент вынес из прошлых тикетов. Перед каждым тикетом он ищет здесь похожий опыт,
        а при ошибке команды — известное решение.</p></div>
      <div class="actions">
        <button class="btn primary" id="kb-add">+ Урок вручную</button>
        <a class="btn" href="/api/knowledge/export?t=${encodeURIComponent(APP.token)}">⬇ Экспорт</a>
        <label class="btn">⬆ Импорт<input type="file" id="kb-import" accept=".json" hidden></label>
      </div>
    </div>
    ${adm("tip", "Как агент учится", `<p>После сеанса ИИ разбирает ход решения и записывает 0–3 урока: <b>рецепт</b> (что сработало),
      <b>ошибка → решение</b> (какая команда упала и что помогло), <b>не делать</b> (что не сработало или вы отклонили).
      Похожие уроки сливаются, а те, что чаще помогают, поднимаются выше. Уроки можно править, отключать и передавать коллегам через экспорт.
      Урок со ссылкой или загрузкой кода из сети агент сохраняет отключённым — проверьте решение и включите его.</p>`)}
    <div id="kb-form" class="hidden" style="margin-top:16px"></div>
    <div class="kb-tools" style="margin-top:16px">
      <input type="search" id="kb-q" placeholder="Поиск: ошибка, программа, симптом…">
      <select id="kb-kind"><option value="">Все типы</option>${Object.entries(KIND).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
    </div>
    <div class="lessons" id="lessons"></div></div>`;
  let timer;
  $("#kb-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadLessons, 250); });
  $("#kb-kind").addEventListener("change", () => loadLessons());
  $("#kb-add").addEventListener("click", () => lessonForm(null));
  $("#kb-import").addEventListener("change", (e) => guarded(async () => {
    const file = e.target.files[0];
    if (!file) return;
    const items = JSON.parse(await file.text());
    const r = await api("/api/knowledge/import", { method: "POST", body: items });
    toast(`Импортировано уроков: ${r.added}`);
    loadLessons();
    loadState();
  }));
  await loadLessons(focus);
}

async function loadLessons(focus = 0) {
  const q = encodeURIComponent($("#kb-q")?.value.trim() || "");
  const kind = encodeURIComponent($("#kb-kind")?.value || "");
  const lessons = await guarded(() => api(`/api/knowledge?q=${q}&kind=${kind}`));
  if (!lessons || !$("#lessons")) return;
  S.lessons = lessons;
  $("#lessons").innerHTML = lessons.length ? lessons.map((l) => `
    <article class="lesson ${l.enabled ? "" : "off"}" id="lesson-${l.id}">
      <div class="top">${kindPill(l.kind)}${l.enabled ? "" : `<span class="pill off" title="Агент не подсказывает этот урок, пока вы его не включите">отключён · на проверке</span>`}<h3>${esc(l.title)}</h3>
        <button class="btn sm ghost" data-edit="${l.id}">Изменить</button>
        <button class="btn sm ghost" data-toggle="${l.id}">${l.enabled ? "Отключить" : "Включить"}</button>
        <button class="btn sm ghost danger" data-del="${l.id}">Удалить</button></div>
      ${l.problem ? `<div class="lbl">Когда</div><div class="txt">${esc(l.problem)}</div>` : ""}
      ${l.solution ? `<div class="lbl">Что делать</div>${codeBlock(l.solution, { title: "решение" })}` : ""}
      <div class="foot">
        ${l.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
        <span class="spacer"></span>
        <span>подсказан ${l.uses} · помог ${l.wins} · встречался ${l.seen}</span>
        ${l.source_ticket ? `<a href="#/t/${l.source_ticket}">из тикета #${l.source_ticket}</a>` : ""}
      </div>
    </article>`).join("")
    : `<div class="empty"><h3>Пока пусто</h3><p>Уроки появятся после первых решённых тикетов — или добавьте свой.</p></div>`;
  $("#lessons").querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => lessonForm(S.lessons.find((l) => l.id === Number(b.dataset.edit)))));
  $("#lessons").querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => guarded(async () => {
    const l = S.lessons.find((x) => x.id === Number(b.dataset.toggle));
    await api(`/api/knowledge/${l.id}`, { method: "PATCH", body: { enabled: !l.enabled } });
    loadLessons();
  })));
  $("#lessons").querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => guarded(async () => {
    if (!confirm("Удалить урок?")) return;
    await api(`/api/knowledge/${b.dataset.del}`, { method: "DELETE" });
    loadLessons();
    loadState();
  })));
  if (focus) $(`#lesson-${focus}`)?.scrollIntoView({ block: "center" });
}

function lessonForm(l) {
  const box = $("#kb-form");
  box.classList.remove("hidden");
  box.innerHTML = `<form class="form card" id="lesson-form">
      <h3>${l ? "Изменить урок" : "Новый урок"}</h3>
      <div class="form-row">
        <label class="field">Заголовок<input name="title" required value="${esc(l?.title || "")}" placeholder="winget не найден на Windows 10"></label>
        <label class="field">Тип<select name="kind">${Object.entries(KIND).map(([k, v]) =>
          `<option value="${k}" ${k === (l?.kind || "manual") ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      </div>
      <label class="field">Когда применять<span class="hint">Симптомы, текст ошибки, версия Windows</span>
        <textarea name="problem" rows="2">${esc(l?.problem || "")}</textarea></label>
      <label class="field">Что делать<span class="hint">Рабочие команды и порядок действий</span>
        <textarea name="solution" rows="3" class="mono">${esc(l?.solution || "")}</textarea></label>
      <label class="field">Теги<input name="tags" value="${esc((l?.tags || []).join(", "))}" placeholder="winget, установка"></label>
      <div class="actions"><button class="btn primary">Сохранить</button><button type="button" class="btn ghost" id="lf-cancel">Отмена</button></div>
    </form>`;
  box.scrollIntoView({ block: "nearest" });
  $("#lf-cancel").addEventListener("click", () => box.classList.add("hidden"));
  $("#lesson-form").addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(async () => {
      const body = Object.fromEntries(new FormData(e.target));
      if (l) await api(`/api/knowledge/${l.id}`, { method: "PATCH", body });
      else await api("/api/knowledge", { method: "POST", body });
      box.classList.add("hidden");
      toast("Урок сохранён");
      loadLessons();
      loadState();
    });
  });
}

/* ================================================================== настройки */

async function viewSettings() {
  const { settings: s, presets } = await api("/api/settings");
  const sel = (v, cur) => (v === cur ? "selected" : "");
  $("#main").innerHTML = `<div class="narrow">
    <div class="page-head"><div><h1>Настройки</h1><p>Модель, поведение агента, AnyDesk и обновления.</p></div></div>
    <form id="settings-form" class="settings form">
      <section class="card form">
        <h2>🧠 Модель</h2>
        <label class="field">Провайдер
          <select name="provider">${Object.entries(presets).map(([k, p]) =>
            `<option value="${k}" ${sel(k, s.provider)}>${esc(p.title)}</option>`).join("")}</select>
        </label>
        <label class="field">Адрес API<input name="base_url" value="${esc(s.base_url)}"></label>
        <label class="field">API-ключ
          <span class="hint" id="key-hint"></span>
          <input name="api_key" type="password" autocomplete="off" placeholder="${s.api_key_set ? "••••••••" : "sk-..."}">
        </label>
        <label class="field">Модель
          <span class="hint">Для скриншотов нужна модель с картинками (Qwen-VL). На OpenRouter бесплатные модели оканчиваются на :free.</span>
          <div class="actions" style="flex-wrap:nowrap">
            <input name="model" list="models" value="${esc(s.model)}">
            <button type="button" class="btn" id="load-models">Список</button>
          </div>
          <datalist id="models"></datalist>
        </label>
        <label class="field">Запасная модель
          <span class="hint">Используется, когда закончился дневной лимит бесплатной модели. Пусто — не переключаться.</span>
          <input name="fallback_model" list="models" value="${esc(s.fallback_model || "")}" placeholder="необязательно">
        </label>
        <div class="form-row">
          <label class="check"><input type="checkbox" name="vision" ${s.vision ? "checked" : ""}> Скриншоты и мышь</label>
          <label class="check"><input type="checkbox" name="json_mode" ${s.json_mode ? "checked" : ""}> Строгий JSON-режим API</label>
        </div>
        <div class="form-row">
          <label class="field">Формат координат
            <select name="coords">
              <option value="pixels" ${sel("pixels", s.coords)}>Пиксели (Qwen2.5-VL, qwen-vl-max)</option>
              <option value="norm1000" ${sel("norm1000", s.coords)}>0..1000 (Qwen3-VL)</option>
            </select>
          </label>
          <label class="field">Лимит ответа, токенов
            <span class="hint">Обычно 4096. Thinking-моделям нужно 4000+; обрезанный ответ программа сама поднимет до 8192.</span>
            <input name="max_tokens" type="number" min="64" max="131072" value="${esc(s.max_tokens)}">
          </label>
        </div>
        <div class="actions"><button type="button" class="btn" id="check-model">Проверить модель</button>
          <span class="muted small">сначала сохраните изменения</span></div>
        <pre class="report hidden" id="check-report"></pre>
      </section>

      <section class="card form">
        <h2>🤖 Агент</h2>
        <label class="field">Что подтверждать вручную
          <select name="confirm">
            <option value="risky" ${sel("risky", s.confirm)}>Только рискованное: установка, удаление, реестр, админ-права</option>
            <option value="all" ${sel("all", s.confirm)}>Каждое действие</option>
            <option value="none" ${sel("none", s.confirm)}>Ничего (полностью автономно)</option>
          </select>
        </label>
        <div class="form-row">
          <label class="field">Язык ответов ИИ<input name="language" value="${esc(s.language)}"></label>
          <label class="field">Максимум шагов<input name="max_steps" type="number" min="5" max="500" value="${s.max_steps}"></label>
        </div>
        <div class="form-row">
          <label class="field">Таймаут команды, сек<span class="hint">Если ИИ не указал свой (установки — до 1800)</span>
            <input name="command_timeout" type="number" min="5" max="1800" value="${s.command_timeout}"></label>
          <label class="field">Хранить скриншоты, дней<span class="hint">Старые удаляются при запуске; 0 — хранить всегда</span>
            <input name="keep_files_days" type="number" min="0" value="${s.keep_files_days}"></label>
        </div>
        <div class="form-row">
          <label class="check"><input type="checkbox" name="learning" ${s.learning ? "checked" : ""}> Самообучение после каждого сеанса</label>
          <label class="field">Уроков из базы в промт<input name="experience_limit" type="number" min="0" max="15" value="${s.experience_limit}"></label>
        </div>
      </section>

      <section class="card form">
        <h2>🖥 AnyDesk</h2>
        <label class="field">Путь к AnyDesk.exe
          <span class="hint">Пусто — найти автоматически; если AnyDesk не установлен, программа скачает его с anydesk.com.</span>
          <input name="anydesk_path" value="${esc(s.anydesk_path)}" placeholder="C:\\Program Files (x86)\\AnyDesk\\AnyDesk.exe">
        </label>
        <div class="form-row">
          <label class="field">Ждать подключения, сек<input name="connect_timeout" type="number" min="10" value="${s.connect_timeout}"></label>
          <label class="field">Заголовок окна сеанса
            <span class="hint">Пусто — найти по ID. Иначе — уникальная часть заголовка окна сеанса (ID или алиас), не «AnyDesk»</span>
            <input name="window_title" value="${esc(s.window_title)}" placeholder="авто по ID"></label>
        </div>
      </section>

      <section class="card form">
        <h2>⬆ Программа и обновления</h2>
        <label class="check"><input type="checkbox" name="auto_update" ${s.auto_update ? "checked" : ""}> Проверять обновления на GitHub при запуске</label>
        <label class="field">Порт веб-интерфейса<span class="hint">Новый порт заработает после перезапуска программы</span>
          <input name="port" type="number" min="1024" max="65535" value="${s.port}"></label>
        <div class="actions">
          <span>Версия <b>${esc(APP.version)}</b></span>
          <button type="button" class="btn" id="upd-check">Проверить обновления</button>
          <button type="button" class="btn green hidden" id="upd-install"></button>
        </div>
        <p class="muted hidden small" id="upd-text" style="margin:0"></p>
      </section>
      <div class="actions"><button class="btn primary lg">Сохранить</button></div>
    </form></div>`;

  const form = $("#settings-form");
  // Подсказка и ссылка «Получить ключ» — для выбранного в форме провайдера, а не сохранённого.
  const keyHint = () => {
    const p = presets[form.provider.value] || {};
    const hint = s.api_key_hint ? ` (${esc(s.api_key_hint)})` : "";
    $("#key-hint").innerHTML = (!s.api_key_set ? "Ключ не задан."
      : form.provider.value !== s.provider
        ? `<b>Сохранён ключ другого провайдера${hint} — вставьте ключ для нового.</b>`
        : `Ключ задан${hint}. Оставьте поле пустым, чтобы не менять.`)
      + (p.key_url ? ` <a href="${esc(p.key_url)}" target="_blank" rel="noopener">Получить ключ →</a>` : "");
  };
  keyHint();
  form.provider.addEventListener("change", () => {
    const p = presets[form.provider.value];
    if (p.base_url) form.base_url.value = p.base_url;
    if (p.model) form.model.value = p.model;
    form.coords.value = p.coords;
    keyHint();
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(async () => {
      const data = Object.fromEntries(new FormData(form));
      for (const name of ["vision", "json_mode", "auto_update", "learning"]) data[name] = form[name].checked;
      await api("/api/settings", { method: "POST", body: data });
      toast(Number(data.port) !== s.port
        ? `Сохранено. Порт ${data.port} заработает после перезапуска программы`
        : "Настройки сохранены");
      await loadState();
      viewSettings();
    });
  });
  $("#load-models").addEventListener("click", () => guarded(async () => {
    // Список сервер берёт по сохранённым адресу и ключу — иначе покажет модели прежнего провайдера.
    if (form.base_url.value.trim() !== s.base_url || form.api_key.value)
      return toast("Сначала сохраните адрес API и ключ — список берётся из сохранённых настроек", true);
    const models = await api("/api/settings/models", { method: "POST" });
    $("#models").innerHTML = models.map((m) =>
      `<option value="${esc(m.id)}">${m.vision === true ? "👁 " : ""}${m.free === true ? "бесплатно" : ""}</option>`).join("");
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
    S.state = await api("/api/update/check", { method: "POST" });
    renderNavbar();
    showUpdate(S.state, true);
  }));
  $("#upd-install").addEventListener("click", async (e) => {
    // Запрос идёт, пока скачивается весь exe (до минуты): без этого кнопку жмут повторно.
    const btn = e.currentTarget;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Скачиваю обновление…";
    try {
      const r = await api("/api/update/install", { method: "POST" });
      btn.textContent = "Перезапуск…"; // кнопка остаётся неактивной до перезагрузки страницы
      toast(r.message);
      setTimeout(() => waitForRestart(APP.version), 3000);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = label;
      toast(err.message, true);
    }
  });
  if (S.state.update) showUpdate(S.state);
}

function showUpdate(st, checked = false) {
  const text = $("#upd-text");
  const btn = $("#upd-install");
  if (!text) return;
  text.classList.remove("hidden");
  if (st.update_error) text.textContent = "Ошибка проверки: " + st.update_error;
  else if (st.update && st.installing) {
    text.textContent = "Скачиваю обновление… программа перезапустится";
    btn.disabled = true; // скачивание уже идёт (из другой вкладки)
    btn.textContent = "Скачиваю обновление…";
    btn.classList.remove("hidden");
  } else if (st.update) {
    text.textContent = `Доступна версия ${st.update.version}. ${st.update.notes || ""}`;
    if (st.frozen) {
      btn.disabled = false;
      btn.textContent = `Обновить до ${st.update.version}`;
      btn.classList.remove("hidden");
    } else text.textContent += " (из исходников обновляйтесь через git pull)";
  } else if (checked) text.textContent = "У вас последняя версия.";
}

// Старый процесс ещё может отвечать, пока не завершился, а «/» без токена отвечает 401 без
// исключения. Поэтому спрашиваем /api/state с токеном и перезагружаемся, когда отвечает уже
// новый процесс: другая версия или сервер успел пропасть (замена exe не удалась — старая версия).
async function waitForRestart(oldVersion, wentDown = false, tries = 0) {
  try {
    const resp = await fetch("/api/state", { cache: "no-store", headers: { "X-Token": APP.token } });
    const st = resp.ok ? await resp.json() : null;
    if (st && (st.version !== oldVersion || wentDown)) return location.reload();
  } catch { wentDown = true; }
  if (tries >= 90) return location.reload(); // ~3 минуты: дальше пусть техник видит, что есть
  setTimeout(() => waitForRestart(oldVersion, wentDown, tries + 1), 2000);
}

/* ================================================================== запуск */

document.addEventListener("click", (e) => {
  unlockAlerts(); // после перезагрузки страницы звук снова разрешается только по клику
  const img = e.target.closest(".ev-step img");
  if (img) {
    $("#lightbox img").src = img.src;
    $("#lightbox").classList.remove("hidden");
  } else if (e.target.closest("#lightbox")) {
    $("#lightbox").classList.add("hidden");
  }
  const exp = e.target.closest("[data-expand]");
  if (exp) {
    const pre = exp.closest(".code").querySelector("pre");
    pre.classList.toggle("expanded");
    exp.textContent = pre.classList.contains("expanded") ? "свернуть" : "развернуть";
  }
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    const pre = copy.closest(".code").querySelector("pre").cloneNode(true);
    pre.querySelector(".prompt")?.remove();
    navigator.clipboard?.writeText(pre.textContent).then(() => {
      copy.textContent = "скопировано ✓";
      setTimeout(() => { copy.textContent = "копировать"; }, 1500);
    }, () => toast("Не удалось скопировать", true));
  }
});
let searchTimer;
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadTickets, 250); });
$("#status-filter").addEventListener("change", loadTickets);
window.addEventListener("hashchange", route);

(async function init() {
  document.documentElement.dataset.theme = storeGet("theme", "dark");
  $("#version").textContent = "v" + APP.version;
  await guarded(loadState);
  await guarded(loadTickets);
  route();
  setInterval(() => { if (S.ticket) refreshTicket().catch(() => {}); }, 1500);
  setInterval(() => { loadTickets().catch(() => {}); loadState().catch(() => {}); }, 4000);
})();
