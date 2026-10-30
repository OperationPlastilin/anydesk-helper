import threading
import time

import pytest

from anydesk_helper import updater
from anydesk_helper.bridge import ConsoleBridge
from anydesk_helper.config import SettingsStore
from anydesk_helper.db import DB
from anydesk_helper.knowledge import KnowledgeBase
from anydesk_helper.session import SessionManager
from anydesk_helper.web import create_app

from .fakes import FakeClipboard, FakeLLM, FakeRemote, FakeTarget, fake_grab

PORT = 8765
HOST = {"Host": f"127.0.0.1:{PORT}"}


REPLIES = [
    {"summary": "Почистить очередь печати", "questions": ["Какой принтер?"], "plan": ["Перезапустить spooler"]},
    {"observation": "", "plan_step": 1, "thought": "перезапускаю", "action": "run",
     "command": "Restart-Service spooler -Force", "risky": True},
    {"observation": "ок", "plan_step": 1, "thought": "", "action": "done", "summary": "Печать работает"},
    {"lessons": [{"kind": "solution", "title": "Зависла очередь печати — перезапуск spooler",
                  "problem": "документы висят в очереди", "solution": "Restart-Service spooler -Force",
                  "tags": ["printer", "принтер"]}]},
]


def make_env(tmp_path, replies=REPLIES, dev=False):
    llms = []

    def factory(session):
        clip = FakeClipboard()
        remote = FakeRemote(clip, {"COMPUTERNAME": "PC: OFFICE-3", "spooler": "Service restarted"})
        bridge = ConsoleBridge(remote, FakeTarget(), clipboard=clip, sleep=lambda s: None)
        llms.append(FakeLLM(list(replies)))
        return {"llm": llms[-1], "target": FakeTarget(), "controller": remote,
                "bridge": bridge, "grab": fake_grab}

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"api_key": "sk-test-1234567890"})
    db = DB(tmp_path / "t.db")
    kb = KnowledgeBase(db)
    manager = SessionManager(db, store, tmp_path, factory=factory, kb=kb)
    app = create_app(db, store, manager, PORT, dev=dev, token="secret", kb=kb)
    client = app.test_client()

    def call(method, path, **kw):
        headers = {**HOST, "X-Token": "secret", **kw.pop("headers", {})}
        return client.open(path, method=method, headers=headers, **kw)

    call.llms, call.manager = llms, manager
    return call, client, db


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def wait_status(db, tid, status, timeout=5):
    end = time.time() + timeout
    while db.get_ticket(tid)["status"] != status and time.time() < end:
        time.sleep(0.05)
    assert db.get_ticket(tid)["status"] == status


def wait_pending(call, tid, kind, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        t = call("GET", f"/api/tickets/{tid}").get_json()
        p = t["session"]["pending"]
        if p and p["kind"] == kind:
            return p
        time.sleep(0.05)
    raise AssertionError(f"не дождались вопроса {kind}: {t}")


def test_requires_token_and_local_host(env):
    call, client, _ = env
    assert client.get("/api/tickets", headers=HOST).status_code == 401
    assert client.get("/api/tickets", headers={"Host": "evil.com", "X-Token": "secret"}).status_code == 403
    # Страница несёт токен — без ключа в ссылке её не отдаём (loopback общий для всех пользователей).
    page = client.get("/", headers=HOST)
    assert page.status_code == 401 and b"secret" not in page.data and "ключом".encode() in page.data
    page = client.get("/?t=secret", headers=HOST)
    assert page.status_code == 200 and b"secret" in page.data
    assert client.get("/?t=wrong", headers=HOST).status_code == 401
    assert client.get("/api/tickets?t=ключ", headers=HOST).status_code == 401  # не 500
    assert client.get("/static/app.js", headers=HOST).status_code == 200
    assert call("GET", "/api/tickets").status_code == 200


def test_anti_framing_headers(env):
    call, client, _ = env
    for r in (client.get("/?t=secret", headers=HOST), call("GET", "/api/state"),
              client.get("/api/state", headers=HOST)):
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert r.headers["Referrer-Policy"] == "no-referrer"


def test_settings_hide_api_key(env):
    call, _, _ = env
    s = call("GET", "/api/settings").get_json()["settings"]
    assert "api_key" not in s and s["api_key_set"] and s["api_key_hint"] == "sk-tes…7890"
    r = call("POST", "/api/settings", json={"api_key": "", "max_steps": "33", "vision": False})
    s = r.get_json()["settings"]
    assert s["max_steps"] == 33 and s["vision"] is False and s["api_key_set"]
    assert call("POST", "/api/settings", json={"confirm": "maybe"}).status_code == 400


def test_full_ticket_flow(env):
    call, _, db = env
    r = call("POST", "/api/tickets", json={"title": "Не печатает принтер", "anydesk_id": "123 456 789",
                                           "description": "Документы висят в очереди"})
    tid = r.get_json()["id"]
    assert call("POST", f"/api/tickets/{tid}/start", json={}).status_code == 200
    assert call("POST", f"/api/tickets/{tid}/start", json={}).status_code == 400  # уже идёт

    p = wait_pending(call, tid, "question")
    assert p["text"] == "Какой принтер?"
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "HP в бухгалтерии"})
    p = wait_pending(call, tid, "plan")
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "yes", "button": True})
    p = wait_pending(call, tid, "confirm")
    assert "Restart-Service" in p["data"]["command"]
    assert call("POST", f"/api/tickets/{tid}/hint", json={"text": "аккуратно"}).status_code == 200
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "yes", "button": True})
    p = wait_pending(call, tid, "done")
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "yes", "button": True})

    wait_status(db, tid, "solved")
    t = call("GET", f"/api/tickets/{tid}").get_json()
    assert t["status"] == "solved" and t["summary"] == "Печать работает"
    assert t["plan"] == ["Перезапустить spooler"]
    kinds = [e["kind"] for e in call("GET", f"/api/tickets/{tid}/events").get_json()]
    for k in ("prompt", "plan", "ask", "answer", "command", "step", "hint", "done"):
        assert k in kinds
    report = call("GET", f"/api/tickets/{tid}/export").get_data(as_text=True)
    assert "Service restarted" in report and "Не печатает принтер" in report
    # самообучение: после решённого тикета в базе опыта появился урок
    lessons = call("GET", "/api/knowledge?q=принтер очередь").get_json()
    assert lessons[0]["title"].startswith("Зависла очередь") and lessons[0]["source_ticket"] == tid
    assert "learned" in kinds
    assert call("GET", "/api/state").get_json()["knowledge"]["lessons"] == 1


def test_stop_session(env):
    call, _, db = env
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    call("POST", f"/api/tickets/{tid}/start", json={})
    wait_pending(call, tid, "question")
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    wait_status(db, tid, "stopped")
    texts = [e["text"] for e in db.events(tid)]
    assert "Останавливаю сеанс…" in texts


def test_pause_and_resume(env):
    call, _, db = env
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    call("POST", f"/api/tickets/{tid}/start", json={})
    p = wait_pending(call, tid, "question")
    assert call("POST", f"/api/tickets/{tid}/control", json={"cmd": "pause"}).status_code == 200
    assert call("GET", f"/api/tickets/{tid}").get_json()["session"]["paused"] is True
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "HP"})
    p = wait_pending(call, tid, "plan")
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "yes", "button": True})
    wait_status(db, tid, "paused")  # агент встал в checkpoint перед первым шагом
    r = call("POST", f"/api/tickets/{tid}/control", json={"cmd": "resume"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert call("GET", f"/api/tickets/{tid}").get_json()["session"]["paused"] is False
    wait_pending(call, tid, "confirm")  # работа продолжилась
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    wait_status(db, tid, "stopped")


def test_no_learning_after_stop(tmp_path):
    replies = [{"summary": "s", "questions": [], "plan": ["шаг"]},
               {"observation": "", "plan_step": 1, "thought": "", "action": "run",
                "command": "Get-Service spooler", "risky": False},
               {"observation": "", "plan_step": 1, "thought": "", "action": "fail", "text": "не могу"},
               {"lessons": [{"kind": "pitfall", "title": "ложный урок", "problem": "p", "solution": "s"}]}]
    call, _, db = make_env(tmp_path, replies)
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    call("POST", f"/api/tickets/{tid}/start", json={})
    p = wait_pending(call, tid, "plan")
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "yes", "button": True})
    wait_pending(call, tid, "fail")
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    wait_status(db, tid, "failed")
    kinds = [e["kind"] for e in db.events(tid)]
    assert "learned" not in kinds and len(call.llms[0].replies) == 1  # рефлексию не запрашивали
    assert call("GET", "/api/knowledge").get_json() == []


def test_delete_ticket_removes_screenshots(env, tmp_path):
    call, _, db = env
    tid = call("POST", "/api/tickets", json={"title": "x"}).get_json()["id"]
    folder = tmp_path / "files" / str(tid)
    folder.mkdir(parents=True)
    (folder / "step_001.jpg").write_bytes(b"jpg")
    other = tmp_path / "files" / str(tid + 1)
    other.mkdir()
    assert call("DELETE", f"/api/tickets/{tid}").status_code == 200
    assert not folder.exists() and other.exists() and db.get_ticket(tid) is None


def test_update_install_blocks_sessions_and_keeps_port(env, monkeypatch):
    call, client, db = env
    started, release, seen = threading.Event(), threading.Event(), []
    monkeypatch.setattr(updater, "check", lambda current: {"version": "9.9.9", "url": "x"})

    def install(info, restart_args=None, exit_after=1.0):
        seen.append(restart_args)
        started.set()
        release.wait(5)

    monkeypatch.setattr(updater, "install", install)
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    result = {}
    other = client.application.test_client()
    th = threading.Thread(target=lambda: result.update(r=other.post(
        "/api/update/install", headers={**HOST, "X-Token": "secret"})))
    th.start()
    assert started.wait(5)
    r = call("POST", f"/api/tickets/{tid}/start", json={})
    assert r.status_code == 400 and "обновление" in r.get_json()["error"]
    assert call("POST", "/api/update/install").status_code == 400  # второе скачивание не начинаем
    assert call("GET", "/api/state").get_json()["installing"] is True
    release.set()
    th.join(5)
    assert result["r"].status_code == 200
    # Новая версия поднимется на том же порту, без браузера (--no-update добавляет updater.install)
    assert seen == [["--no-browser", "--port", str(PORT)]]
    assert db.get_ticket(tid)["status"] == "new"


def test_update_install_error_allows_sessions_again(tmp_path, monkeypatch):
    call, _, db = make_env(tmp_path, dev=True)
    seen = []
    monkeypatch.setattr(updater, "check", lambda current: {"version": "9.9.9", "url": "x"})

    def install(info, restart_args=None, exit_after=1.0):
        seen.append(restart_args)
        raise updater.UpdateError("Не удалось скачать обновление: нет прав")

    monkeypatch.setattr(updater, "install", install)
    r = call("POST", "/api/update/install")
    assert r.status_code == 400 and "нет прав" in r.get_json()["error"]
    assert seen == [["--no-browser", "--port", str(PORT), "--dev"]]
    assert call("GET", "/api/state").get_json()["installing"] is False
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    assert call("POST", f"/api/tickets/{tid}/start", json={}).status_code == 200
    wait_pending(call, tid, "question")
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    wait_status(db, tid, "stopped")


def test_start_needs_anydesk_id(env):
    call, _, _ = env
    tid = call("POST", "/api/tickets", json={"title": "без ID"}).get_json()["id"]
    r = call("POST", f"/api/tickets/{tid}/start", json={})
    assert r.status_code == 400 and "AnyDesk" in r.get_json()["error"]


def test_knowledge_crud(env):
    call, _, _ = env
    r = call("POST", "/api/knowledge", json={"title": "Сначала проверяй кабель", "problem": "нет сети",
                                             "solution": "Test-NetConnection", "tags": "сеть, net"})
    lid = r.get_json()["id"]
    assert r.status_code == 201 and r.get_json()["kind"] == "manual"
    r = call("PATCH", f"/api/knowledge/{lid}", json={"enabled": False, "solution": "ipconfig /all"})
    assert r.get_json()["enabled"] is False and r.get_json()["solution"] == "ipconfig /all"
    exported = call("GET", "/api/knowledge/export").get_json()
    assert exported[0]["title"] == "Сначала проверяй кабель"
    assert call("POST", "/api/knowledge/import", json={"x": 1}).status_code == 400
    assert call("DELETE", f"/api/knowledge/{lid}").status_code == 200
    assert call("GET", "/api/knowledge").get_json() == []


def test_state_shows_active_ask(env):
    call, _, db = env
    assert call("GET", "/api/state").get_json()["active_ask"] is None
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    call("POST", f"/api/tickets/{tid}/start", json={})
    p = wait_pending(call, tid, "question")
    ask = call("GET", "/api/state").get_json()["active_ask"]
    assert ask == {"id": p["id"], "kind": "question", "text": "Какой принтер?"}
    call("POST", f"/api/tickets/{tid}/answer", json={"ask_id": p["id"], "value": "HP"})
    p2 = wait_pending(call, tid, "plan")
    assert call("GET", "/api/state").get_json()["active_ask"]["id"] == p2["id"] != p["id"]
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    wait_status(db, tid, "stopped")
    assert call("GET", "/api/state").get_json()["active_ask"] is None


def test_manual_lessons_are_never_merged(env):
    call, _, _ = env
    lesson = {"title": "Сначала проверяй кабель", "problem": "нет сети", "solution": "Test-NetConnection"}
    a = call("POST", "/api/knowledge", json=lesson).get_json()
    b = call("POST", "/api/knowledge", json={**lesson, "solution": "ipconfig /all"}).get_json()
    assert a["id"] != b["id"] and len(call("GET", "/api/knowledge").get_json()) == 2
    assert b["solution"] == "ipconfig /all"


def test_api_404_is_json(env):
    call, _, _ = env
    for path in ("/api/tickets/999", "/api/tickets/999/export", "/api/nope"):
        r = call("GET", path)
        assert r.status_code == 404 and r.is_json, path
    assert call("GET", "/api/tickets/999").get_json()["error"] == "Тикет не найден"
    assert call("PATCH", "/api/knowledge/999", json={}).get_json()["error"] == "Урок не найден"
    assert call("GET", "/api/nope").get_json()["error"] == "Не найдено"
    r = call("GET", "/files/999/a.jpg")
    assert r.status_code == 404 and not r.is_json  # не API — обычная страница
