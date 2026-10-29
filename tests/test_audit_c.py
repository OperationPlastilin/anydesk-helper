"""Сеанс, настройки и запуск программы: исправления по аудиту (группа C)."""

import json
import os
import pathlib
import socket
import threading
import time

import pytest

from anydesk_helper import __main__ as main_mod
from anydesk_helper import anydesk, session as session_mod
from anydesk_helper.agent import StopSession
from anydesk_helper.bridge import ConsoleBridge
from anydesk_helper.config import PRESETS, Settings, SettingsStore, validate
from anydesk_helper.db import DB
from anydesk_helper.knowledge import KnowledgeBase
from anydesk_helper.session import Session, SessionManager
from anydesk_helper.web import create_app

from .fakes import FakeClipboard, FakeLLM, FakeRemote, FakeTarget, fake_grab


def manager(tmp_path, factory=None, **settings):
    store = SettingsStore(tmp_path / "settings.json")
    if settings:
        store.update(settings)
    db = DB(tmp_path / "t.db")
    kb = KnowledgeBase(db)
    return SessionManager(db, store, tmp_path, factory=factory or session_mod.default_factory, kb=kb)


def wait_done(m, timeout=5):
    end = time.time() + timeout
    while m.current.alive and time.time() < end:
        if m.current.pending:  # план и прочие вопросы — одобряем
            m.current.answer(m.current.pending["id"], "yes")
        time.sleep(0.02)
    assert not m.current.alive


# ---------------------------------------------------------------- настройки


def test_load_skips_invalid_stored_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"provider": "groq", "port": null, "max_steps": Infinity, "coords": "norm",'
                    ' "model": "qwen-x", "api_key": null, "confirm": "all"}', encoding="utf-8")
    s = SettingsStore(path).load(use_env=False)
    assert s.provider == "openrouter" and s.port == 8765 and s.max_steps == 80 and s.coords == "pixels"
    assert s.model == "qwen-x" and s.confirm == "all" and s.api_key == ""
    for broken in ("[1, 2]", '"text"', "null", "{", "\xff\xfe"):
        path.write_text(broken, encoding="latin-1")
        assert SettingsStore(path).load() == Settings(api_key=SettingsStore(path).load().api_key)
    # Порт вне диапазона уронил бы main() в port_in_use (OverflowError) ещё до интерфейса.
    for bad in ({"port": 70000}, {"port": 0}, {"port": -1}, {"max_steps": 0}, {"temperature": "nan"},
                {"keep_files_days": -5}, {"base_url": "openrouter.ai/api/v1"}):
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert SettingsStore(path).load(use_env=False) == Settings(), bad


def test_validate_stays_strict_for_the_form(tmp_path):
    with pytest.raises(ValueError):
        validate({"provider": "groq"})
    with pytest.raises(ValueError):
        validate({"max_steps": float("inf")})
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"confirm": "maybe"})
    assert store.update({"fallback_model": " qwen/qwen3:free "}).fallback_model == "qwen/qwen3:free"
    assert Settings().fallback_model == "" and Settings().max_tokens == 4096
    for bad in ({"port": 70000}, {"port": "0"}, {"max_steps": "-3"}, {"base_url": "ftp://x"},
                {"base_url": "localhost:11434/v1"}):
        with pytest.raises(ValueError):
            store.update(bad)
    assert validate({"port": "1024", "base_url": "http://localhost:11434/v1", "keep_files_days": 0}) == {
        "port": 1024, "base_url": "http://localhost:11434/v1", "keep_files_days": 0}
    assert validate({"base_url": ""}) == {"base_url": ""}  # «Другой сервер» до ввода адреса
    # Плохое значение в файле при следующем сохранении из формы заменяется нормальным.
    data = json.loads(store.path.read_text(encoding="utf-8"))
    store.path.write_text(json.dumps({**data, "provider": "groq"}), encoding="utf-8")
    store.update({"max_steps": 10})
    assert json.loads(store.path.read_text(encoding="utf-8"))["provider"] == "openrouter"


def test_switching_provider_drops_old_key(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"api_key": "sk-or-1234567890"})
    # Тот же провайдер, пустое поле ключа (форма шлёт все поля) — ключ остаётся.
    s = store.update({"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1/", "api_key": ""})
    assert s.api_key == "sk-or-1234567890"
    # Другой провайдер без нового ключа — ключ OpenRouter на сервер DashScope не уйдёт.
    s = store.update({"provider": "dashscope", "base_url": PRESETS["dashscope"]["base_url"], "api_key": ""})
    assert s.api_key == "" and not s.public()["api_key_set"]
    # С новым ключом — сохраняется новый.
    s = store.update({"provider": "openrouter", "base_url": PRESETS["openrouter"]["base_url"],
                      "api_key": "sk-new-1234567890"})
    assert s.api_key == "sk-new-1234567890"
    # Свой сервер: другой хост — ключ сбрасывается, другой путь на том же хосте — нет.
    store.update({"provider": "custom", "base_url": "https://llm.local/v1", "api_key": "sk-local-123456"})
    assert store.update({"base_url": "https://llm.local/api/v1"}).api_key == "sk-local-123456"
    assert store.update({"base_url": "https://evil.example/v1"}).api_key == ""


# ---------------------------------------------------------------- сеанс


def test_resume_method_is_not_shadowed(tmp_path):
    m = manager(tmp_path)
    t = m.db.create_ticket("x")
    s = Session(m, t, resume=True)
    assert s.resume_requested is True and callable(s.resume)
    s.pause()
    s.resume()
    assert s.paused is False


def test_ask_beeps_through_manager_hook(tmp_path):
    m = manager(tmp_path)
    beeped = threading.Event()
    m.alert = beeped.set
    s = Session(m, m.db.create_ticket("x"))
    threading.Timer(0.05, lambda: s.answer(s.pending["id"], "да")).start()
    assert s.ask("question", "Какой принтер?") == "да" and beeped.wait(2)


def test_hanging_beep_does_not_delay_the_answer(tmp_path):
    # Без звуковой карты MessageBeep может висеть секундами — ответ всё равно принимается сразу.
    m = manager(tmp_path)
    release = threading.Event()
    m.alert = lambda: release.wait(10)
    s = Session(m, m.db.create_ticket("x"))
    threading.Timer(0.05, lambda: s.answer(s.pending["id"], "да")).start()
    start = time.monotonic()
    assert s.ask("question", "Какой принтер?") == "да"
    assert time.monotonic() - start < 2
    release.set()

    def broken():
        raise RuntimeError("нет звуковой карты")

    m.alert = broken  # сигнал не должен мешать вопросу
    threading.Timer(0.05, lambda: s.answer(s.pending["id"], "нет")).start()
    assert s.ask("question", "Ещё?") == "нет"
    session_mod.beep()  # вне Windows — ничего не делает и не падает


def test_cleanup_survives_locked_folder(tmp_path, monkeypatch):
    old = tmp_path / "files" / "12" / "a.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"jpg")
    month_ago = time.time() - 40 * 86400
    os.utime(old, (month_ago, month_ago))

    def locked(self):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(pathlib.Path, "rmdir", locked)
    m = manager(tmp_path)  # уборка при запуске не роняет программу
    assert not old.exists() and old.parent.exists()
    assert m.cleanup_files(30) == 0


def test_delete_files(tmp_path):
    m = manager(tmp_path)
    (m.files_dir / "7").mkdir()
    (m.files_dir / "7" / "step_001.jpg").write_bytes(b"jpg")
    m.delete_files(7)
    m.delete_files(8)  # папки нет — не ошибка
    assert not (m.files_dir / "7").exists()


def test_wait_window_stops_at_once(tmp_path, monkeypatch):
    m = manager(tmp_path)
    s = Session(m, m.db.create_ticket("x", anydesk_id="123"))
    calls, slept = [], []
    s.sleep = slept.append

    def find(address, hint):
        calls.append(address)
        if len(calls) == 3:
            s.stop()  # техник нажал «Стоп», пока ждём окно
        return None

    monkeypatch.setattr(anydesk, "find_session_window", find)
    with pytest.raises(StopSession):
        session_mod.wait_window(s, "123", 90)
    assert len(calls) == 3 and slept == [2, 2, 2]


def test_wait_window_timeout_and_found(tmp_path, monkeypatch):
    m = manager(tmp_path)
    s = Session(m, m.db.create_ticket("x", anydesk_id="123"))
    calls, slept = [], []
    s.sleep = slept.append
    monkeypatch.setattr(anydesk, "find_session_window", lambda a, h: calls.append(a))
    assert session_mod.wait_window(s, "123", 5) is None  # 3 проверки за 5 с, без лишней паузы в конце
    assert len(calls) == 3 and slept == [2, 2]
    calls[:] = []
    assert session_mod.wait_window(s, "123", 90) is None and len(calls) == 46
    monkeypatch.setattr(anydesk, "find_session_window", lambda a, h: "окно" if len(calls) > 50 else calls.append(a))
    assert session_mod.wait_window(s, "123", 90) == "окно"


def test_find_target_honours_stop_before_connect(tmp_path, monkeypatch):
    m = manager(tmp_path)
    s = Session(m, m.db.create_ticket("x", anydesk_id="123 456 789"))
    monkeypatch.setattr(anydesk, "find_executable", lambda *a: "AnyDesk.exe")
    connected = []
    monkeypatch.setattr(anydesk, "connect", lambda *a: connected.append(a))
    s.stopping = True
    with pytest.raises(StopSession):
        session_mod.find_target(s, m.settings())
    assert connected == []


def test_default_factory_wiring(tmp_path, monkeypatch):
    m = manager(tmp_path)
    t = m.db.create_ticket("x", anydesk_id="1")
    target = FakeTarget()
    monkeypatch.setattr(session_mod, "find_target", lambda session, s: target)
    monkeypatch.setenv("COMPUTERNAME", "TECH-PC")
    s = Session(m, t)
    parts = session_mod.default_factory(s)
    assert parts["controller"].target is target and parts["bridge"].local_name == "TECH-PC"
    assert parts["llm"].should_stop() is False
    parts["llm"].notify("переключаюсь на запасную модель")  # техник видит смену модели в ленте
    assert ("warning", "переключаюсь на запасную модель") in [(e["kind"], e["text"]) for e in m.db.events(t["id"])]
    s.stopping = True
    assert parts["llm"].should_stop() is True  # «Стоп» прерывает повторы запроса к модели

    local = session_mod.default_factory(Session(m, t, mode="local"))
    assert local["bridge"].local_name is None  # режим «этот ПК»: консоль и есть ПК техника
    assert isinstance(local["controller"].target, anydesk.ScreenTarget)


class FailSafeException(Exception):
    """Как pyautogui.FailSafeException: мышь увели в угол экрана."""


def failsafe_factory(fail_on):
    llm = FakeLLM([
        {"summary": "s", "questions": [], "plan": ["шаг"]},
        {"observation": "", "plan_step": 1, "thought": "", "action": "run", "command": "Get-Service", "risky": False},
        {"observation": "", "plan_step": 1, "thought": "", "action": "run", "command": "Get-Process", "risky": False},
        {"lessons": [{"kind": "pitfall", "title": "ложный урок", "problem": "p", "solution": "s"}]},
    ])

    class Remote(FakeRemote):
        def paste_line(self, line):
            if len(self.commands) == fail_on:
                raise FailSafeException("PyAutoGUI fail-safe triggered from mouse moving to a corner")
            super().paste_line(line)

    def factory(session):
        clip = FakeClipboard()
        remote = Remote(clip)
        bridge = ConsoleBridge(remote, FakeTarget(), clipboard=clip, sleep=lambda s: None)
        return {"llm": llm, "target": FakeTarget(), "controller": remote, "bridge": bridge, "grab": fake_grab}

    return factory, llm


def test_emergency_stop_mid_session_is_stopped_without_learning(tmp_path):
    factory, llm = failsafe_factory(fail_on=2)  # проба и первая команда прошли, вторая — мышь в углу
    m = manager(tmp_path, factory)
    t = m.db.create_ticket("x", anydesk_id="1")
    m.start(t["id"])
    wait_done(m)
    assert m.db.get_ticket(t["id"])["status"] == "stopped"
    kinds = [e["kind"] for e in m.db.events(t["id"])]
    assert "learned" not in kinds and "error" not in kinds
    assert len(llm.replies) == 1 and m.kb.list() == []  # рефлексию не запрашивали


def test_failsafe_outside_agent_is_stopped(tmp_path):
    def factory(session):
        raise FailSafeException("PyAutoGUI fail-safe triggered")

    m = manager(tmp_path, factory)
    t = m.db.create_ticket("x", anydesk_id="1")
    m.start(t["id"])
    wait_done(m)
    assert m.db.get_ticket(t["id"])["status"] == "stopped"
    assert "Аварийная остановка: мышь в углу экрана" in [e["text"] for e in m.db.events(t["id"])]


# ---------------------------------------------------------------- запуск программы


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYDESK_HELPER_DATA", str(tmp_path))
    monkeypatch.setattr(anydesk, "enable_dpi_awareness", lambda: None)
    monkeypatch.setattr(anydesk, "set_console_title", lambda title: None)
    opened = []
    monkeypatch.setattr(main_mod.webbrowser, "open", opened.append)

    def no_update(store, argv):
        raise AssertionError("второй запуск не должен обновляться: exe занят работающей программой")

    monkeypatch.setattr(main_mod, "startup_update", no_update)
    monkeypatch.setattr(main_mod, "port_in_use", lambda port: True)
    return opened


def test_second_launch_opens_own_instance_without_update(launch, tmp_path, monkeypatch):
    probes = []
    monkeypatch.setattr(main_mod, "own_instance", lambda port, token: probes.append((port, token)) or True)
    assert main_mod.main(["--port", "9000"]) == 0
    token = (tmp_path / "token").read_text(encoding="ascii")
    assert probes == [(9000, token)] and launch == [f"http://127.0.0.1:9000/?t={token}"]
    assert main_mod.app_token(tmp_path) == token  # ключ постоянный: закладки переживают перезапуск


def test_cli_port_out_of_range_is_a_usage_error(capsys):
    assert main_mod.parse_args(["--port", "9000"]).port == 9000
    for bad in ("70000", "0", "abc"):
        with pytest.raises(SystemExit):
            main_mod.parse_args(["--port", bad])
    assert "порт" in capsys.readouterr().err


def test_busy_port_of_another_user_is_not_opened(launch, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "own_instance", lambda port, token: False)
    assert main_mod.main([]) == 1
    assert launch == [] and "занят другой программой" in capsys.readouterr().out


def test_own_instance_probe(tmp_path):
    from werkzeug.serving import make_server

    m = manager(tmp_path)
    holder = {}
    srv = make_server("127.0.0.1", 0, lambda environ, start: holder["app"](environ, start), threaded=True)
    port = srv.server_port
    holder["app"] = create_app(m.db, m.store, m, port, token="t" * 32, kb=m.kb)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert main_mod.own_instance(port, "t" * 32) is True
        assert main_mod.own_instance(port, "x" * 32) is False  # чужой экземпляр (другой пользователь)
    finally:
        srv.shutdown()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert main_mod.own_instance(port, "x" * 32) is False  # никто не слушает


def test_app_token_regenerates_bad_file(tmp_path):
    (tmp_path / "token").write_text("short", encoding="ascii")
    token = main_mod.app_token(tmp_path)
    assert len(token) >= 24 and token != "short" and (tmp_path / "token").read_text() == token


def test_own_instance_on_non_http_port():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def banner():
            conn, _ = srv.accept()
            with conn:
                conn.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
                conn.recv(1024)

        threading.Thread(target=banner, daemon=True).start()
        assert main_mod.own_instance(port, "x" * 32) is False  # BadStatusLine -> чужая программа


def test_question_is_published_after_waiting_status(tmp_path):
    # Кто видит вопрос (pending), тот уже видит статус «ждёт ответа» и событие ask.
    m = manager(tmp_path)
    t = m.db.create_ticket("x")
    s = Session(m, t)
    seen = {}

    def answer_when_visible():
        while not s.pending:
            time.sleep(0.001)
        seen["status"] = m.db.get_ticket(t["id"])["status"]
        seen["ask_logged"] = any(e["kind"] == "ask" for e in m.db.events(t["id"]))
        s.answer(s.pending["id"], "да")

    threading.Thread(target=answer_when_visible, daemon=True).start()
    assert s.ask("question", "Какой принтер?") == "да"
    assert seen == {"status": "waiting", "ask_logged": True}
