"""Локальный веб-интерфейс: тикеты, ход решения, вопросы ИИ, настройки, обновления.

Безопасность: сервер слушает только 127.0.0.1. Каждый запрос к API должен нести токен
(X-Token или ?t=). Страницу «/» тоже отдаём только по ссылке с токеном, которую открывает
программа: loopback общий для всех пользователей Windows, а токен лежит в их личной папке
данных. Чужой сайт в том же браузере токен не прочитает и не встроит интерфейс во фрейм
(кликджекинг кнопки «Выполнить»). Проверка Host защищает от DNS rebinding.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import NotFound

from . import updater
from .agent import NO, STOP, YES
from .config import PRESETS
from .llm import LLM, LLMError
from .paths import resource_dir
from .report import ticket_report
from .version import __version__

ANSWERS = {"yes": YES, "no": NO, "stop": STOP}

NO_TOKEN_PAGE = """<!doctype html><meta charset="utf-8"><title>AnyDesk Helper</title>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px">
<h2>Нужна ссылка с ключом доступа</h2>
<p>Откройте интерфейс из программы: запустите AnyDeskHelper.exe ещё раз — она откроет браузер
с правильной ссылкой. Ссылка с ключом (…/?t=…) также напечатана в окне программы.</p></body>"""


def create_app(db, store, manager, port: int, dev: bool = False, token: str | None = None,
               kb=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    token = token or secrets.token_urlsafe(24)
    static = resource_dir() / "anydesk_helper" / "static"
    if not static.is_dir():
        static = Path(__file__).resolve().parent / "static"
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    # installing — скачивается обновление: после него процесс завершится, новый сеанс не начинаем.
    update_state: dict = {"info": None, "error": "", "installing": False}
    update_lock = threading.Lock()  # «запуск сеанса» и «начало обновления» не пересекаются

    def token_ok() -> bool:
        given = request.headers.get("X-Token") or request.args.get("t") or ""
        return secrets.compare_digest(given.encode(), token.encode())

    @app.before_request
    def guard():
        if request.host not in allowed_hosts:
            abort(403)
        if request.path == "/" and not token_ok():
            return Response(NO_TOKEN_PAGE, 401, mimetype="text/html")
        if request.path.startswith(("/api/", "/files/")) and not token_ok():
            abort(401)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")  # токен из ?t= не уходит по ссылкам
        return resp

    @app.errorhandler(ValueError)
    def bad_value(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(404)
    def not_found(e):
        if not request.path.startswith("/api/"):
            return e
        custom = e.description != NotFound.description
        return jsonify(error=e.description if custom else "Не найдено"), 404

    def ticket_or_404(tid: int) -> dict:
        t = db.get_ticket(tid)
        if not t:
            abort(404, "Тикет не найден")
        return t

    def body() -> dict:
        return request.get_json(silent=True) or {}

    # ------------------------------------------------------------ страница

    @app.get("/")
    def index():
        html = (static / "index.html").read_text(encoding="utf-8")
        html = html.replace("__TOKEN__", token).replace("__VERSION__", __version__)
        html = html.replace("__DEV__", "true" if dev else "false")
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(static, name, max_age=0)

    # ------------------------------------------------------------ тикеты

    @app.get("/api/tickets")
    def tickets():
        return jsonify(db.list_tickets(request.args.get("q", ""), request.args.get("status", "")))

    @app.post("/api/tickets")
    def create_ticket():
        b = body()
        title = str(b.get("title", "")).strip()
        if not title:
            raise ValueError("Укажите кратко, что нужно сделать")
        t = db.create_ticket(title, str(b.get("description", "")).strip(),
                             str(b.get("anydesk_id", "")).strip())
        db.add_event(t["id"], "status", "Тикет создан")
        return jsonify(t), 201

    @app.get("/api/tickets/<int:tid>")
    def get_ticket(tid):
        t = ticket_or_404(tid)
        s = manager.get(tid)
        t["session"] = {"active": bool(s), "paused": bool(s and s.paused),
                        "pending": s.pending if s else None}
        return jsonify(t)

    @app.patch("/api/tickets/<int:tid>")
    def edit_ticket(tid):
        ticket_or_404(tid)
        fields = {k: str(v) for k, v in body().items()
                  if k in ("title", "description", "anydesk_id", "status")}
        return jsonify(db.update_ticket(tid, **fields))

    @app.delete("/api/tickets/<int:tid>")
    def delete_ticket(tid):
        if manager.get(tid):
            raise ValueError("Сначала остановите сеанс")
        db.delete_ticket(tid)
        manager.delete_files(tid)  # и скриншоты экрана пользователя
        return jsonify(ok=True)

    @app.get("/api/tickets/<int:tid>/events")
    def events(tid):
        return jsonify(db.events(tid, int(request.args.get("after", 0))))

    @app.get("/api/tickets/<int:tid>/export")
    def export(tid):
        t = ticket_or_404(tid)
        text = ticket_report(t, db.events(tid))
        return Response(text, mimetype="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="ticket-{tid}.md"'})

    @app.get("/files/<int:tid>/<name>")
    def files(tid, name):
        return send_from_directory(manager.files_dir / str(tid), name)

    # ------------------------------------------------------------ сеанс

    @app.post("/api/tickets/<int:tid>/start")
    def start(tid):
        t = ticket_or_404(tid)
        b = body()
        mode = "local" if dev and b.get("mode") == "local" else "anydesk"
        if mode == "anydesk" and not t["anydesk_id"]:
            raise ValueError("Укажите в тикете ID AnyDesk")
        s = store.load()
        if not s.api_key and "openrouter" in s.base_url:
            raise ValueError("Не задан API-ключ модели (Настройки)")
        with update_lock:
            if update_state["installing"]:
                raise ValueError("Идёт обновление программы — дождитесь перезапуска")
            try:
                manager.start(tid, password=str(b.get("password", "")), connect=bool(b.get("connect", True)),
                              mode=mode, dry_run=bool(dev and b.get("dry_run")),
                              resume=bool(b.get("resume") and t["plan"]))
            except RuntimeError as e:
                raise ValueError(str(e)) from None
        return jsonify(ok=True)

    @app.post("/api/tickets/<int:tid>/answer")
    def answer(tid):
        s = manager.get(tid)
        b = body()
        if not s:
            raise ValueError("Сеанс не активен")
        value = str(b.get("value", ""))
        value = ANSWERS.get(value, value) if b.get("button") else value.strip()
        if not s.answer(int(b.get("ask_id", 0)), value):
            raise ValueError("На этот вопрос уже ответили")
        return jsonify(ok=True)

    @app.post("/api/tickets/<int:tid>/control")
    def control(tid):
        s = manager.get(tid)
        if not s:
            raise ValueError("Сеанс не активен")
        cmd = body().get("cmd")
        if cmd == "pause":
            s.pause()
        elif cmd == "resume":
            s.resume()
        elif cmd == "stop":
            s.stop()
        else:
            raise ValueError("Неизвестная команда")
        return jsonify(ok=True)

    @app.post("/api/tickets/<int:tid>/hint")
    def hint(tid):
        s = manager.get(tid)
        text = str(body().get("text", "")).strip()
        if not text:
            raise ValueError("Пустая подсказка")
        if s:
            s.hint(text)
        else:
            db.add_event(tid, "note", text)  # заметка к закрытому тикету
        return jsonify(ok=True)

    # ------------------------------------------------------------ настройки

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings=store.load().public(), presets=PRESETS)

    @app.post("/api/settings")
    def save_settings():
        return jsonify(settings=store.update(body()).public())

    @app.post("/api/settings/models")
    def models():
        try:
            return jsonify(LLM(store.load()).list_models())
        except LLMError as e:
            raise ValueError(str(e)) from None

    @app.post("/api/settings/check")
    def check():
        from .checks import check_model

        settings = store.load()
        llm = LLM(settings)
        notes: list[str] = []
        llm.notify = notes.append  # автовыбор/замена модели — показываем в отчёте проверки
        llm.on_model_change = lambda model: store.update({"model": model})
        ok, report = check_model(llm, settings)
        return jsonify(ok=ok, report=report[:1] + [f"ℹ {n}" for n in notes] + report[1:])

    # ------------------------------------------------------------ база опыта

    def need_kb():
        if kb is None:
            abort(404, "База опыта недоступна")
        return kb

    @app.get("/api/knowledge")
    def knowledge():
        return jsonify(need_kb().list(request.args.get("q", ""), request.args.get("kind", "")))

    @app.post("/api/knowledge")
    def knowledge_add():
        b = body()
        # Урок, введённый техником, — всегда отдельной записью, не сливаем с похожим.
        lesson = need_kb().add(str(b.get("kind", "manual")), str(b.get("title", "")),
                               str(b.get("problem", "")), str(b.get("solution", "")), b.get("tags", ""),
                               dedup=False)
        return jsonify(lesson), 201

    @app.patch("/api/knowledge/<int:lid>")
    def knowledge_edit(lid):
        if not need_kb().get(lid):
            abort(404, "Урок не найден")
        return jsonify(kb.update(lid, **body()))

    @app.delete("/api/knowledge/<int:lid>")
    def knowledge_delete(lid):
        need_kb().delete(lid)
        return jsonify(ok=True)

    @app.get("/api/knowledge/export")
    def knowledge_export():
        return Response(need_kb().export(), mimetype="application/json", headers={
            "Content-Disposition": 'attachment; filename="anydesk-helper-knowledge.json"'})

    @app.post("/api/knowledge/import")
    def knowledge_import():
        items = request.get_json(silent=True)
        if not isinstance(items, list):
            raise ValueError("Ожидается JSON-массив уроков (файл экспорта базы опыта)")
        return jsonify(added=need_kb().import_(items))

    # ------------------------------------------------------------ состояние и обновления

    @app.get("/api/state")
    def state():
        cur = manager.current if manager.current and manager.current.alive else None
        p = cur.pending if cur else None  # читаем один раз: поток сеанса может сбросить его
        s = store.load()
        counts = db.status_counts()
        return jsonify(version=__version__, active_ticket=cur.tid if cur else None,
                       active_status=db.get_ticket(cur.tid)["status"] if cur else None,
                       active_ask={"id": p["id"], "kind": p["kind"], "text": p["text"][:200]} if p else None,
                       update=update_state["info"], update_error=update_state["error"],
                       installing=update_state["installing"],
                       frozen=updater.can_self_update(), counts=counts,
                       knowledge=kb.stats() if kb else None,
                       model={"name": s.model, "provider": s.provider, "key": bool(s.api_key),
                              "vision": s.vision})

    @app.post("/api/update/check")
    def update_check():
        try:
            update_state["info"] = updater.check(__version__)
            update_state["error"] = ""
        except updater.UpdateError as e:
            update_state["error"] = str(e)
        return state()

    @app.post("/api/update/install")
    def update_install():
        with update_lock:
            if update_state["installing"]:
                raise ValueError("Обновление уже скачивается")
            if manager.current and manager.current.alive:
                raise ValueError("Дождитесь окончания сеанса")
            update_state["installing"] = True
        # Новая версия поднимется на том же порту (его опрашивает открытая вкладка) и в том же режиме.
        restart = ["--no-browser", "--port", str(port)] + (["--dev"] if dev else [])
        try:
            info = update_state["info"] or updater.check(__version__)
            if not info:
                raise ValueError("Обновлений нет")
            updater.install(info, restart_args=restart)
        except updater.UpdateError as e:
            update_state["installing"] = False
            raise ValueError(str(e)) from None
        except BaseException:
            update_state["installing"] = False
            raise
        return jsonify(ok=True, message="Обновление установлено, программа перезапускается")

    app.config["UPDATE_STATE"] = update_state
    app.config["TOKEN"] = token
    return app
