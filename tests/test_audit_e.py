"""Фронтенд (static/app.js): гонки опроса, перерисовка, фокус вопроса, сигнал «ждёт ответа».

app.js исполняется в Node (vm) на крошечной подделке DOM: элементы ищутся по селектору,
fetch отвечает вручную в нужном порядке — без таймеров и реальных пауз. Нет node — тесты пропускаются.
"""

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from anydesk_helper.config import Settings

APP_JS = Path(__file__).resolve().parent.parent / "anydesk_helper" / "static" / "app.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="нужен Node.js")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [appPath, scenarioPath] = process.argv.slice(2);

class El {
  constructor(sel, tag = "div") {
    Object.assign(this, { sel, tagName: tag.toUpperCase(), type: "", _inner: "", writes: 0, inserted: [],
      dataset: {}, listeners: {}, kids: {}, value: "", textContent: "", title: "", disabled: false,
      scrollTop: 0, scrollHeight: 0, clientHeight: 0, scrolled: 0 });
    this.classList = { toggle() {}, add() {}, remove() {}, contains: () => false };
  }
  get innerHTML() { return this._inner; }
  set innerHTML(v) { this._inner = v; this.writes++; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  insertAdjacentHTML(_, html) { this.inserted.push(html); }
  querySelector(s) { return this.kids[s] || null; }
  querySelectorAll(s) { return this.kids[s] ? [this.kids[s]] : []; }
  contains(el) { return el === this || Object.values(this.kids).includes(el); }
  focus() { doc.activeElement = this; }
  click() { (this.listeners.click || []).forEach((f) => f({ currentTarget: this, target: this })); }
  scrollIntoView() { this.scrolled++; }
  closest() { return null; }
  matches(sel) {
    return sel.split(",").some((part) => {
      part = part.trim();
      if (part.split(":")[0] !== this.tagName.toLowerCase()) return false;
      return ![...part.matchAll(/:not\(\[type=(\w+)\]\)/g)].some((m) => m[1] === this.type);
    });
  }
}

const els = {};
const doc = {
  title: "", documentElement: { dataset: {} }, focused: true,
  querySelector: (s) => (s in els ? els[s] : (els[s] = new El(s))),
  querySelectorAll: () => [], addEventListener() {},
  hasFocus() { return this.focused; },
};
doc.body = new El("body", "body");
doc.activeElement = doc.body;

const reqs = [];
const shown = [];
class Notification {
  constructor(title, opts) { this.title = title; this.opts = opts; this.closed = false; shown.push(this); }
  close() { this.closed = true; }
}
Notification.permission = "granted";

const ctx = vm.createContext({
  document: doc, location: { hash: "", reload() {} }, APP: { token: "t", version: "0", dev: false },
  navigator: {}, console, Notification, confirm: () => true, setTimeout: () => 0, clearTimeout() {},
  setInterval: () => 0, setImmediate, Promise, Set, Object, String, Number, JSON, Error,
  FormData: class { *[Symbol.iterator]() { yield ["title", "Принтер"]; } },
  fetch: (path, opts = {}) => new Promise((resolve, reject) =>
    reqs.push({ path, method: opts.method || "GET", headers: opts.headers || {}, body: opts.body, resolve, reject })),
});
ctx.window = ctx;
ctx.addEventListener = () => {};
ctx.focus = () => {};

const h = {
  El, els, doc, reqs, shown,
  open: (part) => reqs.filter((r) => !r.done && r.path.includes(part)),
  respond(r, body, status = 200, type = "application/json") {
    r.done = true;
    r.resolve({ status, ok: status < 400, headers: { get: () => type },
                json: async () => body, text: async () => String(body) });
  },
  fail(r) { r.done = true; r.reject(new TypeError("Failed to fetch")); },
  tick: () => new Promise((r) => setImmediate(r)),
  async settle(n = 8) { for (let i = 0; i < n; i++) await h.tick(); },
  ev: (id, kind = "info") => ({ id, kind, text: "e" + id, ts: "2026-10-29T10:00:00", data: {} }),
};
ctx.h = h;

vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });
const scenario = fs.readFileSync(scenarioPath, "utf8");
vm.runInContext(`(async () => { ${scenario} })()`, ctx).then(
  (out) => { process.stdout.write(JSON.stringify(out ?? null)); },
  (err) => { console.error(err && err.stack || err); process.exit(1); });
"""


def run_js(tmp_path, scenario: str):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    script = tmp_path / "scenario.js"
    script.write_text(scenario, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(APP_JS), str(script)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_app_js_syntax():
    proc = subprocess.run([NODE, "--check", str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_settings_form_fields_are_known_settings():
    """Каждое поле формы настроек — настоящее поле Settings (иначе сервер молча его выбросит)."""
    src = APP_JS.read_text(encoding="utf-8")
    view = src[src.index("async function viewSettings"):src.index("function showUpdate")]
    names = set(re.findall(r'name="(\w+)"', view))
    known = {f.name for f in dataclasses.fields(Settings)}
    assert {"fallback_model", "max_tokens"} <= names  # C5 «Запасная модель», #63 лимит ответа
    assert names <= known, names - known


# ---------------------------------------------------------------- #48 параллельные опросы ленты

@needs_node
def test_overlapping_polls_do_not_duplicate_events(tmp_path):
    out = run_js(tmp_path, """
      S.ticketId = 1; S.lastEventId = 0; S.ticket = { plan: [] };
      const p1 = pollEvents(), p2 = pollEvents();
      const [r1, r2] = h.open("/events");
      const evs = [h.ev(1), h.ev(2), h.ev(3)];
      h.respond(r2, evs.concat([h.ev(4)]));  // второй (свежий) ответ пришёл раньше
      await h.tick();
      h.respond(r1, evs);                     // первый, устаревший, — позже
      await Promise.all([p1, p2]);
      return { n: $("#timeline").inserted.length, last: S.lastEventId, urls: [r1.path, r2.path] };
    """)
    assert out["urls"] == ["/api/tickets/1/events?after=0"] * 2
    assert out == {**out, "n": 4, "last": 4}  # ни задвоения, ни отката lastEventId


@needs_node
def test_stale_poll_after_reopening_ticket_is_dropped(tmp_path):
    out = run_js(tmp_path, """
      S.ticketId = 1; S.lastEventId = 5; S.ticket = { plan: [] };
      const old = pollEvents();            // after=5, ещё до ухода со страницы
      S.lastEventId = 0;                   // тикет открыли заново (viewTicket)
      const fresh = pollEvents();          // after=0
      const [rOld, rFresh] = h.open("/events");
      h.respond(rOld, [h.ev(6)]);
      await h.tick();
      h.respond(rFresh, [1, 2, 3, 4, 5, 6].map((i) => h.ev(i)));
      await Promise.all([old, fresh]);
      return $("#timeline").inserted.map((x) => /e(\\d+)/.exec(x)[1]);
    """)
    assert out == ["1", "2", "3", "4", "5", "6"]  # история с начала, без пропуска и задвоения


# ---------------------------------------------------------------- #49 перерисовка без изменений

@needs_node
def test_unchanged_header_and_widget_are_not_rebuilt(tmp_path):
    out = run_js(tmp_path, """
      S.state = { active_ticket: 1, active_status: "running", counts: {}, model: {} };
      S.ticketId = 1;
      S.ticket = { id: 1, title: "Принтер", status: "running", plan: [], session: { active: true, paused: false } };
      renderTicketMeta(); renderNavbar();
      renderTicketMeta(); renderNavbar();
      const same = [$("#t-info").writes, $("#t-actions").writes, $("#agent-widget").writes, $("#plan").writes];
      S.ticket.session.paused = true; S.state.active_status = "paused";
      renderTicketMeta(); renderNavbar();
      return { same, changed: [$("#t-actions").writes, $("#agent-widget").writes], info: $("#t-info").writes };
    """)
    assert out["same"] == [1, 1, 1, 1]
    assert out["changed"] == [2, 2]  # кнопка «Пауза» сменилась на «Продолжить»
    assert out["info"] == 1


# ---------------------------------------------------------------- #50/#78 фокус и Enter в вопросе

PENDING_SETUP = """
  S.ticketId = 1;
  const box = $("#pending");
  const input = $("#ans-text"); input.tagName = "INPUT";
  const yes = new h.El('[data-btn="yes"]', "button"); yes.textContent = "Выполнить";
  const send = new h.El("[data-send]", "button");
  Object.assign(box.kids, { "#ans-text": input, '[data-btn="yes"]': yes, "[data-btn]": yes, "[data-send]": send });
  S.ticket = { session: { pending: { id: 7, kind: "confirm", text: "?", data: { command: "Remove-Item C:\\\\x" } } } };
  const enter = () => input.listeners.keydown[0]({ key: "Enter", shiftKey: false, ctrlKey: false, preventDefault() {} });
"""


@needs_node
def test_new_question_does_not_steal_focus_from_hint(tmp_path):
    out = run_js(tmp_path, PENDING_SETUP + """
      const hint = $("#hint-text"); hint.tagName = "TEXTAREA"; hint.focus();
      renderPending();
      return { active: document.activeElement.sel, scrolled: box.scrolled };
    """)
    assert out == {"active": "#hint-text", "scrolled": 0}


@needs_node
def test_new_question_takes_focus_when_nobody_types(tmp_path):
    out = run_js(tmp_path, PENDING_SETUP + """
      renderPending();
      return { active: document.activeElement.sel, scrolled: box.scrolled };
    """)
    assert out == {"active": "#ans-text", "scrolled": 1}


@needs_node
def test_enter_in_empty_confirm_moves_to_yes_without_answering(tmp_path):
    out = run_js(tmp_path, PENDING_SETUP + """
      renderPending();
      enter();
      const afterEmpty = { active: document.activeElement.sel, answers: h.open("/answer").length };
      input.value = "сначала сделай копию";
      input.focus();
      enter();
      const r = h.open("/answer")[0];
      return { afterEmpty, sent: r && r.path };
    """)
    assert out["afterEmpty"] == {"active": '[data-btn="yes"]', "answers": 0}
    assert out["sent"] == "/api/tickets/1/answer"  # с текстом Enter отправляет подсказку, как раньше


# ---------------------------------------------------------------- #55 тикет не найден

@needs_node
def test_missing_ticket_shows_empty_state(tmp_path):
    out = run_js(tmp_path, """
      S.ticketId = 9;
      const p = viewTicket(9);
      h.respond(h.open("/api/tickets/9")[0], "<h1>Not Found</h1>", 404, "text/html");
      await p;
      return $("#main").innerHTML;
    """)
    assert "Тикет #9 не открылся" in out and "удалён" in out and "#/tickets" in out


# ---------------------------------------------------------------- #56 кнопка «Создать тикет»

@needs_node
def test_create_ticket_button_disabled_while_request_runs(tmp_path):
    out = run_js(tmp_path, """
      viewNew();
      const form = $("#new-form"), btn = new h.El("button");
      const p = form.listeners.submit[0]({ preventDefault() {}, submitter: btn, target: form });
      await h.tick();
      const during = btn.disabled;
      h.respond(h.open("/api/tickets")[0], { id: 5 }, 201);
      await h.tick(); await h.tick();
      h.respond(h.open("/api/tickets?")[0], []);
      for (let i = 0; i < 5; i++) await h.tick();
      return { during, after: btn.disabled, posts: h.reqs.filter((r) => r.method === "POST").length,
               hash: location.hash };
    """)
    assert out == {"during": True, "after": False, "posts": 1, "hash": "#/t/5"}


# ---------------------------------------------------------------- #71 сигнал «ждёт ответа»

@needs_node
def test_notify_each_ask_once_and_not_when_watching(tmp_path):
    out = run_js(tmp_path, """
      document.focused = false; S.ticketId = null;
      notifyAsk(3, { id: 1, kind: "plan", text: "Почистить очередь" });
      notifyAsk(3, { id: 1, kind: "plan", text: "Почистить очередь" });   // тот же вопрос
      notifyAsk(3, { id: 2, kind: "confirm", text: "Restart-Service spooler" });
      document.focused = true; S.ticketId = 3;
      notifyAsk(3, { id: 3, kind: "done", text: "Готово?" });              // техник смотрит на тикет
      return h.shown.map((n) => [n.title, n.opts.tag, n.opts.requireInteraction]);
    """)
    assert out == [["Тикет #3: Подтвердите план", "aih-ask", True],
                   ["Тикет #3: Подтвердите действие", "aih-ask", True]]


@needs_node
def test_waiting_session_of_other_ticket_notifies_and_marks_title(tmp_path):
    out = run_js(tmp_path, """
      document.focused = false; S.route = "kb"; S.ticketId = null;
      S.state = { active_ticket: 4, active_status: "waiting", counts: {}, model: {} };
      renderNavbar();
      const title = document.title;
      const w = watchAsk();   // сервер без active_ask — вопрос берём из тикета
      h.respond(h.open("/api/tickets/4")[0], { id: 4, session: { pending: { id: 11, kind: "question", text: "Какой принтер?" } } });
      await w;
      S.state = { active_ticket: 4, active_status: "waiting", active_ask: { id: 12, kind: "confirm", text: "cmd" } };
      await watchAsk();       // сервер с active_ask — без лишнего запроса
      S.state.active_status = "running"; S.state.counts = {}; S.state.model = {};
      renderNavbar();
      return { title, after: document.title, shown: h.shown.map((n) => n.opts.body),
               ticketFetches: h.reqs.filter((r) => r.path === "/api/tickets/4").length };
    """)
    assert out["title"].startswith("❓ Ждёт ответа") and "#4" in out["title"]
    assert out["after"] == "AnyDesk Helper"
    assert out["shown"] == ["Какой принтер?", "cmd"]
    assert out["ticketFetches"] == 1


@needs_node
def test_new_ask_closes_previous_notification_and_renotifies(tmp_path):
    """#71: тот же tag без renotify браузер подменяет молча; устаревшее «Подтвердите план» закрываем."""
    out = run_js(tmp_path, """
      document.focused = false; S.ticketId = null;
      notifyAsk(3, { id: 1, kind: "plan", text: "План" });
      notifyAsk(3, { id: 2, kind: "confirm", text: "Restart-Service spooler" });
      const afterSecond = h.shown.map((n) => [n.closed, n.opts.renotify, n.opts.tag]);
      // на вопрос ответили в другой вкладке: в /api/state вопроса больше нет
      S.state = { active_ticket: 3, active_status: "running", active_ask: null };
      await watchAsk();
      const afterAnswer = h.shown[1].closed;
      notifyAsk(3, { id: 3, kind: "done", text: "Готово?" });
      S.state = { active_ticket: null };       // сеанс кончился
      await watchAsk();
      return { afterSecond, afterAnswer, last: h.shown[2].closed };
    """)
    assert out["afterSecond"] == [[True, True, "aih-ask"], [False, True, "aih-ask"]]
    assert out["afterAnswer"] is True and out["last"] is True


@needs_node
def test_open_ticket_without_pending_closes_only_its_notification(tmp_path):
    out = run_js(tmp_path, """
      document.focused = false; S.ticketId = null;
      notifyAsk(4, { id: 1, kind: "question", text: "Какой принтер?" });
      S.ticketId = 9; S.ticket = { session: {} };   // открыт другой тикет без вопроса
      renderPending();
      const other = h.shown[0].closed;
      S.ticketId = 4; S.ticket = { session: {} };   // открыт тот самый тикет: ответили
      renderPending();
      return { other, own: h.shown[0].closed };
    """)
    assert out == {"other": False, "own": True}


PRESS = """
  // Как браузер: Enter на кнопке в фокусе нажимает её, если keydown не отменён.
  const press = (el, extra = {}) => {
    let prevented = false;
    (el.listeners.keydown || []).forEach((f) => f({ key: "Enter", shiftKey: false, ctrlKey: false,
      repeat: false, ...extra, preventDefault() { prevented = true; } }));
    if (!prevented && document.activeElement === el) el.click();
  };
  const realNow = Date.now; let shift = 0; Date.now = () => realNow() + shift;
  yes.dataset.btn = "yes";
"""


@needs_node
def test_held_enter_does_not_press_yes_after_focus_moves(tmp_path):
    """#78: автоповтор удержанного Enter и дребезг сразу после перевода фокуса «Выполнить» не нажимают."""
    out = run_js(tmp_path, PENDING_SETUP + PRESS + """
      renderPending();
      press(input);                          // пустое поле: фокус на «Выполнить»
      press(yes, { repeat: true });          // клавишу держат — автоповтор
      press(yes);                            // дребезг в первые 300 мс
      const held = h.open("/answer").length;
      shift = 400;
      press(yes);                            // новое, осознанное нажатие
      const r = h.open("/answer")[0];
      return { active: document.activeElement.sel, held, sent: r && JSON.parse(r.body) };
    """)
    assert out["active"] == '[data-btn="yes"]' and out["held"] == 0
    assert out["sent"] == {"ask_id": 7, "value": "yes", "button": True}


@needs_node
def test_error_ask_focuses_retry_but_ignores_repeat(tmp_path):
    out = run_js(tmp_path, PENDING_SETUP + PRESS + """
      h.els["#ans-text"] = null;             // у вопроса error нет поля ввода
      S.ticket.session.pending = { id: 8, kind: "error", text: "Не удалось вывести окно AnyDesk…", data: {} };
      renderPending();
      press(yes, { repeat: true });
      const held = h.open("/answer").length;
      shift = 400;
      press(yes);
      return { active: document.activeElement.sel, held, sent: h.open("/answer").length,
               title: /adm-title">[^<]*/.exec(box.innerHTML)[0] };
    """)
    assert out["active"] == '[data-btn="yes"]' and out["held"] == 0 and out["sent"] == 1
    assert "Ошибка — повторить?" in out["title"] and "модели" not in out["title"]


@needs_node
def test_command_footer_explains_busy_and_refused(tmp_path):
    out = run_js(tmp_path, """
      const foot = (status, extra = {}) => /<span class="spacer"><\\/span>\\s*(<span[^>]*>[^<]*<\\/span>)/
        .exec(renderEvent({ id: 1, kind: "command", text: "Get-Date", ts: "2026-10-29T10:00:00",
                             data: { command: "Get-Date", status, ...extra } }))[1];
      return { done: foot("done", { exit_code: 0 }), running: foot("running"), busy: foot("busy"),
               refused: foot("refused"), no_ack: foot("no_ack") };
    """)
    assert "exit-ok" in out["done"] and "код 0" in out["done"]
    assert "выполняется" in out["running"] and "exit-bad" not in out["running"]
    assert "предыдущая команда ещё выполняется" in out["busy"] and "exit-bad" not in out["busy"]
    assert "консоль на компьютере техника" in out["refused"] and "exit-bad" in out["refused"]
    assert "консоль не ответила" in out["no_ack"] and "exit-bad" in out["no_ack"]


@needs_node
def test_start_and_update_chip_locked_while_update_installs(tmp_path):
    out = run_js(tmp_path, """
      S.ticket = { id: 1, title: "x", status: "new", plan: [], anydesk_id: "1", session: {} };
      S.state = { counts: {}, model: {}, installing: true, update: { version: "2.0" } };
      renderStartCard(); renderNavbar();
      const during = { card: $("#start-card").innerHTML, chip: $("#update-chip").textContent };
      S.state.installing = false;
      renderStartCard(); renderNavbar();
      return { during, after: $("#start-card").innerHTML, chip: $("#update-chip").textContent };
    """)
    assert re.search(r'id="st-start"\s+disabled', out["during"]["card"])
    assert "Скачиваю обновление… программа перезапустится" in out["during"]["card"]
    assert "Обновление" in out["during"]["chip"]
    assert not re.search(r'id="st-start"\s+disabled', out["after"]) and "Скачиваю" not in out["after"]
    assert out["chip"] == "⬆ v2.0"


@needs_node
def test_wait_for_restart_reloads_only_for_new_process(tmp_path):
    """«/» без токена отвечает 401 без исключения: ждём /api/state нового процесса."""
    out = run_js(tmp_path, """
      const timers = []; setTimeout = (f) => { timers.push(f); return 0; };
      let reloads = 0; location.reload = () => { reloads++; };
      const start = h.reqs.length;           // запрос init() к /api/state висит — он не наш
      const mine = () => h.reqs.slice(start);
      const step = async (fn) => { fn(mine().find((r) => !r.done)); await h.settle(); };
      const log = [];
      waitForRestart("1.0");
      await step((r) => h.respond(r, { version: "1.0" }));      // старый процесс ещё жив
      log.push(reloads);
      timers.shift()(); await step((r) => h.respond(r, "no", 401, "text/html"));
      log.push(reloads);
      timers.shift()(); await step((r) => h.respond(r, { version: "1.1" }));
      log.push(reloads, timers.length);
      // замена exe не удалась: сервер пропал и вернулся той же версией — тоже перезагружаем
      waitForRestart("1.0");
      await step((r) => h.fail(r));
      log.push(reloads);
      timers.shift()(); await step((r) => h.respond(r, { version: "1.0" }));
      log.push(reloads);
      return { log, paths: [...new Set(mine().map((r) => r.path))],
               tokens: [...new Set(mine().map((r) => r.headers["X-Token"]))] };
    """)
    assert out == {"log": [0, 0, 1, 0, 1, 2], "paths": ["/api/state"], "tokens": ["t"]}


@needs_node
def test_disabled_lesson_is_marked_for_review(tmp_path):
    out = run_js(tmp_path, """
      const l = (id, enabled) => ({ id, enabled, kind: "solution", title: "t" + id, problem: "", solution: "",
                                    tags: [], uses: 0, wins: 0, seen: 1, source_ticket: null });
      const p = loadLessons();
      h.respond(h.open("/api/knowledge")[0], [l(1, true), l(2, false)]);
      await p;
      return $("#lessons").innerHTML.split("<article").slice(1).map((a) => a.includes("на проверке"));
    """)
    assert out == [False, True]
