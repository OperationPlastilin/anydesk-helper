"""Тесты исправлений: таймаут из настроек, Стоп прерывает команду, продолжение сеанса,
очистка скриншотов, шифрование API-ключа."""

import json
import os
import sys
import time

import pytest

from anydesk_helper import config
from anydesk_helper.actions import parse_action
from anydesk_helper.agent import STOP, YES, StopSession, restore_state
from anydesk_helper.config import SettingsStore
from anydesk_helper.db import DB
from anydesk_helper.session import SessionManager

from .test_agent import make_agent


def test_command_timeout_comes_from_settings():
    assert parse_action({"action": "run", "command": "dir"}).timeout is None
    replies = [{"summary": "", "questions": [], "plan": ["x"]},
               {"action": "run", "command": "Get-Date"},
               {"action": "run", "command": "winget install X", "timeout": 900},
               {"action": "done", "summary": "ok"}]
    agent, _, _, _ = make_agent(replies, [YES], confirm="none")
    agent.s.command_timeout = 77
    seen = []
    orig = agent.bridge.run
    agent.bridge.run = lambda cmd, timeout=120: (seen.append(timeout), orig(cmd, timeout))[1]
    assert agent.run() is True
    assert seen[1:] == [77, 900]  # seen[0] — проба системы


def test_stop_interrupts_running_command():
    replies = [{"summary": "", "questions": [], "plan": ["x"]},
               {"action": "run", "command": "Start-Process setup.exe -Wait", "timeout": 5},
               {"action": "ask_operator", "question": "Ждать дальше?"}]
    agent, _, op, remote = make_agent(replies, [YES, STOP], confirm="none")
    remote.outputs = {"COMPUTERNAME": "PC: X"}
    remote.running = r"setup\.exe"
    with pytest.raises(StopSession):
        agent.run()
    assert ("ctrl", "c") in remote.keys and agent.bridge.pending is None
    assert any("прервана" in e[1] for e in op.events)


def test_restore_state_and_resume():
    events = [
        {"kind": "command", "text": "probe", "data": {"status": "done", "output": "PC"}},
        {"kind": "plan", "text": "", "data": {"plan": ["Проверить", "Установить"]}},
        {"kind": "ask", "text": "Какая лицензия?", "data": {"ask_id": 1, "ask_kind": "question"}},
        {"kind": "answer", "text": "M365", "data": {"ask_id": 1, "ask_kind": "question", "value": "M365"}},
        {"kind": "ask", "text": "План?", "data": {"ask_id": 2, "ask_kind": "plan"}},
        {"kind": "answer", "text": "", "data": {"ask_id": 2, "ask_kind": "plan", "value": "yes"}},
        {"kind": "step", "text": "", "data": {"step": 1, "description": "run: Get-Package *Office*"}},
        {"kind": "command", "text": "", "data": {"status": "done", "exit_code": 0, "output": "нет\nOffice"}},
        {"kind": "hint", "text": "ставь 64-бит", "data": {}},
    ]
    saved = restore_state(events)
    assert saved["plan"] == ["Проверить", "Установить"]
    assert saved["notes"] == ["Q: Какая лицензия? A: M365", "ставь 64-бит"]
    assert saved["history"] == ["1. run: Get-Package *Office* -> done, exit 0: нет Office"]

    replies = [{"action": "done", "summary": "ok"}]  # план не составляется заново — сразу шаги
    agent, llm, op, _ = make_agent(replies, [YES, YES])
    assert agent.run(saved) is True
    first = llm.requests[0][1]["content"]
    assert "M365" in first and "Get-Package" in first and "interrupted" in first
    # сохранённый план техник подтверждает заново: он мог быть так и не одобрен
    assert [a[0] for a in op.asks] == ["plan", "done"]


def test_cleanup_old_screenshots(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.update({"keep_files_days": 0})
    mgr = SessionManager(DB(tmp_path / "t.db"), store, tmp_path)
    old = mgr.files_dir / "1" / "old.jpg"
    new = mgr.files_dir / "2" / "new.jpg"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"jpg")
    month_ago = time.time() - 40 * 86400
    os.utime(old, (month_ago, month_ago))
    assert mgr.cleanup_files(0) == 0 and old.exists()
    assert mgr.cleanup_files(30) == 1
    assert not old.exists() and not old.parent.exists() and new.exists()


def test_api_key_is_encrypted_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "dpapi_available", lambda: True)
    monkeypatch.setattr(config, "_dpapi", lambda data, encrypt: bytes(b ^ 0x5A for b in data))
    store = SettingsStore(tmp_path / "s.json")
    store.update({"api_key": "sk-or-secret-123456"})
    raw = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["api_key"]
    assert raw.startswith("dpapi:") and "secret" not in raw
    assert store.load().api_key == "sk-or-secret-123456"
    store.update({"max_steps": 10})  # повторное сохранение не шифрует дважды
    assert store.load().api_key == "sk-or-secret-123456"

    def broken(data, encrypt):
        raise OSError("другой пользователь")
    monkeypatch.setattr(config, "_dpapi", broken)
    assert store.load().api_key == ""  # ключ с чужого ПК не читается — просим ввести заново


def test_plain_key_from_old_file_still_works(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"api_key": "sk-plain"}), encoding="utf-8")
    assert SettingsStore(tmp_path / "s.json").load().api_key == "sk-plain"


@pytest.mark.skipif(sys.platform != "win32", reason="настоящий DPAPI есть только в Windows")
def test_real_dpapi_roundtrip():
    enc = config.protect("sk-проверка-123")
    assert enc.startswith("dpapi:") and config.unprotect(enc) == "sk-проверка-123"
