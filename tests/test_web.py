import time

import pytest

from anydesk_helper.bridge import ConsoleBridge
from anydesk_helper.config import SettingsStore
from anydesk_helper.db import DB
from anydesk_helper.session import SessionManager
from anydesk_helper.web import create_app

from .fakes import FakeClipboard, FakeLLM, FakeRemote, FakeTarget, fake_grab

PORT = 8765
HOST = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def env(tmp_path):
    replies = [
        {"summary": "Почистить очередь печати", "questions": ["Какой принтер?"], "plan": ["Перезапустить spooler"]},
        {"observation": "", "plan_step": 1, "thought": "перезапускаю", "action": "run",
         "command": "Restart-Service spooler -Force", "risky": True},
        {"observation": "ок", "plan_step": 1, "thought": "", "action": "done", "summary": "Печать работает"},
    ]

    def factory(session):
        clip = FakeClipboard()
        remote = FakeRemote(clip, {"COMPUTERNAME": "PC: OFFICE-3", "spooler": "Service restarted"})
        bridge = ConsoleBridge(remote, FakeTarget(), clipboard=clip, sleep=lambda s: None)
        return {"llm": FakeLLM(replies), "target": FakeTarget(), "controller": remote,
                "bridge": bridge, "grab": fake_grab}

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"api_key": "sk-test-1234567890"})
    db = DB(tmp_path / "t.db")
    manager = SessionManager(db, store, tmp_path, factory=factory)
    app = create_app(db, store, manager, PORT, token="secret")
    client = app.test_client()

    def call(method, path, **kw):
        headers = {**HOST, "X-Token": "secret", **kw.pop("headers", {})}
        return client.open(path, method=method, headers=headers, **kw)

    return call, client, db


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
    page = client.get("/", headers=HOST)
    assert page.status_code == 200 and b"secret" in page.data
    assert call("GET", "/api/tickets").status_code == 200


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

    end = time.time() + 5
    while db.get_ticket(tid)["status"] != "solved" and time.time() < end:
        time.sleep(0.05)
    t = call("GET", f"/api/tickets/{tid}").get_json()
    assert t["status"] == "solved" and t["summary"] == "Печать работает"
    assert t["plan"] == ["Перезапустить spooler"]
    kinds = [e["kind"] for e in call("GET", f"/api/tickets/{tid}/events").get_json()]
    for k in ("prompt", "plan", "ask", "answer", "command", "step", "hint", "done"):
        assert k in kinds
    report = call("GET", f"/api/tickets/{tid}/export").get_data(as_text=True)
    assert "Service restarted" in report and "Не печатает принтер" in report


def test_stop_session(env):
    call, _, db = env
    tid = call("POST", "/api/tickets", json={"title": "x", "anydesk_id": "1"}).get_json()["id"]
    call("POST", f"/api/tickets/{tid}/start", json={})
    wait_pending(call, tid, "question")
    call("POST", f"/api/tickets/{tid}/control", json={"cmd": "stop"})
    end = time.time() + 5
    while db.get_ticket(tid)["status"] != "stopped" and time.time() < end:
        time.sleep(0.05)
    assert db.get_ticket(tid)["status"] == "stopped"


def test_start_needs_anydesk_id(env):
    call, _, _ = env
    tid = call("POST", "/api/tickets", json={"title": "без ID"}).get_json()["id"]
    r = call("POST", f"/api/tickets/{tid}/start", json={})
    assert r.status_code == 400 and "AnyDesk" in r.get_json()["error"]
