"""Сеанс решения тикета в фоновом потоке + связь агента с веб-интерфейсом."""

from __future__ import annotations

import itertools
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

from . import anydesk
from .agent import NO, STOP, YES, Agent, StopSession, restore_state
from .bridge import ConsoleBridge
from .controller import Controller
from .llm import LLM
from .paths import resource_dir


class Session:
    """Один запущенный тикет. Методы event/ask/checkpoint/image — «оператор» для агента."""

    _ids = itertools.count(1)

    def __init__(self, manager, ticket: dict, password: str = "", connect: bool = True,
                 mode: str = "anydesk", dry_run: bool = False, resume: bool = False):
        self.m = manager
        self.ticket = ticket
        self.tid = ticket["id"]
        self.password = password  # в базу не пишем
        self.connect = connect
        self.mode = mode
        self.dry_run = dry_run
        # продолжить по сохранённому плану, а не начинать заново (resume() — снятие с паузы)
        self.resume_requested = resume
        self.pending: dict | None = None
        self._answers: queue.Queue = queue.Queue()
        self.paused = False
        self.stopping = False
        self._hints: list[str] = []
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"ticket-{self.tid}", daemon=True)

    # ------------------------------------------------------------ оператор для агента

    def event(self, kind: str, text: str = "", **data) -> None:
        self.m.db.add_event(self.tid, kind, text, **data)
        if kind == "plan":
            self.m.db.update_ticket(self.tid, plan=data.get("plan", []))

    def status(self, status: str) -> None:
        self.m.db.update_ticket(self.tid, status=status)

    def ask(self, kind: str, text: str, **data) -> str:
        pid = next(self._ids)
        self.pending = {"id": pid, "kind": kind, "text": text, "data": data}
        self.event("ask", text, ask_kind=kind, ask_id=pid, **data)
        self.status("waiting")
        try:
            self.m.alert()  # звук слышен, даже если вкладка браузера закрыта
        except Exception:
            pass
        try:
            while True:
                if self.stopping:
                    return STOP
                try:
                    answer_id, answer = self._answers.get(timeout=0.3)
                except queue.Empty:
                    continue
                if answer_id == pid:
                    break
        finally:
            self.pending = None
        self.event("answer", _answer_text(kind, answer), ask_kind=kind, ask_id=pid, value=answer)
        if answer != STOP:
            self.status("paused" if self.paused else "running")
        return answer

    def answer(self, ask_id: int, value: str) -> bool:
        pending = self.pending
        if not pending or pending["id"] != ask_id:
            return False
        self._answers.put((ask_id, value))
        return True

    def checkpoint(self) -> list[str]:
        if self.paused and not self.stopping:
            self.status("paused")
            while self.paused and not self.stopping:
                time.sleep(0.3)
            if not self.stopping:
                self.status("running")
        if self.stopping:
            raise StopSession
        with self._lock:
            hints, self._hints = self._hints, []
        return hints

    def image(self, step: int, img) -> str:
        folder = self.m.files_dir / str(self.tid)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"step_{step:03d}_{int(time.time())}.jpg"
        img.convert("RGB").save(folder / name, quality=80)
        return name

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stopping:
            time.sleep(min(0.3, end - time.monotonic()))

    # ------------------------------------------------------------ управление из веба

    def hint(self, text: str) -> None:
        with self._lock:
            self._hints.append(text)
        self.event("hint", text)

    def pause(self) -> None:
        self.paused = True
        self.event("info", "Пауза: агент остановится после текущего шага")

    def resume(self) -> None:
        self.paused = False
        self.event("info", "Продолжение работы")

    def stop(self) -> None:
        if not self.stopping:
            self.event("info", "Останавливаю сеанс…")
        self.stopping = True
        self.paused = False

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    # ------------------------------------------------------------ поток сеанса

    def _run(self) -> None:
        self.status("running")
        self.event("status", "Сеанс начат")
        result_status, summary, agent = "failed", "", None
        settings = self.m.settings()
        try:
            parts = self.m.factory(self)
            agent = Agent(settings, parts["llm"], parts["target"], parts["controller"],
                          parts["bridge"], self, self.ticket, playbook_dirs=self.m.playbook_dirs,
                          grab=parts.get("grab") or _default_grab(), sleep=self.sleep, kb=self.m.kb)
            saved = restore_state(self.m.db.events(self.tid)) if self.resume_requested else None
            ok = agent.run(saved)
            result_status = "solved" if ok else "failed"
        except StopSession:
            result_status = "stopped"
            self.event("status", "Сеанс остановлен техником")
        except Exception as e:  # сеанс не должен ронять программу
            if type(e).__name__ == "FailSafeException":  # pyautogui: мышь в углу — аварийная остановка
                result_status = "stopped"
                self.event("status", "Аварийная остановка: мышь в углу экрана")
            else:
                self.event("error", f"Ошибка: {e}", traceback=traceback.format_exc())
        finally:
            if self.m.kb:
                self.m.kb.mark_outcome(self.tid, result_status == "solved")
            # Самообучение: разбираем сеанс и запоминаем уроки (кроме остановленных вручную:
            # после «Стопа» модель больше не вызываем).
            if agent and settings.learning and not self.stopping and result_status in ("solved", "failed"):
                try:
                    self.event("info", "Агент разбирает сеанс и записывает уроки в базу опыта…")
                    agent.learn(result_status)
                except Exception as e:
                    self.event("warning", f"Самообучение не удалось: {e}")
            for e in reversed(self.m.db.events(self.tid)):
                if e["kind"] in ("done", "failed"):
                    summary = e["text"]
                    break
            self.m.db.update_ticket(self.tid, status=result_status, summary=summary)
            self.event("status", {"solved": "Задача решена", "failed": "Задача не решена",
                                  "stopped": "Остановлено"}[result_status], result=result_status)


def _answer_text(kind: str, value: str) -> str:
    if value == YES:
        return "Да" if kind != "plan" else "План одобрен"
    if value == NO:
        return "Нет"
    if value == STOP:
        return "Стоп"
    return value or "(пусто)"


def _default_grab():
    from .screen import grab

    return grab


def default_factory(session: Session) -> dict:
    """Собирает «железо» сеанса: окно AnyDesk, мышь/клавиатуру, консольный мост, модель."""
    s = session.m.settings()
    local_name = None
    if session.mode == "local":
        target = anydesk.ScreenTarget()
        session.event("warning", "Режим проверки: агент управляет ЭТИМ компьютером")
    else:
        target = find_target(session, s)
        # Имя ПК техника: если ввод всё же попадёт в его консоль, обёртка команду не выполнит.
        local_name = os.environ.get("COMPUTERNAME")
    controller = Controller(dry_run=session.dry_run, target=target)
    bridge = ConsoleBridge(controller, target, interrupted=lambda: session.stopping, local_name=local_name)
    llm = LLM(s)
    llm.should_stop = lambda: session.stopping  # «Стоп» прерывает повторы запроса и паузы между ними
    llm.notify = lambda text: session.event("warning", text)  # смена на запасную модель, рост max_tokens
    return {"llm": llm, "target": target, "controller": controller, "bridge": bridge}


def beep() -> None:
    """Сигнал «ИИ ждёт ответа» (только Windows)."""
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONQUESTION)
    except Exception:
        pass


def find_target(session: Session, s):
    address = anydesk.normalize_address(session.ticket.get("anydesk_id", ""))
    if not address:
        raise RuntimeError("В тикете не указан ID AnyDesk")
    if session.connect:
        try:
            exe = anydesk.find_executable(s.anydesk_path, [session.m.data_dir / "anydesk"])
        except FileNotFoundError:
            session.event("info", "AnyDesk не найден — скачиваю с официального сайта")
            exe = anydesk.download(session.m.data_dir / "anydesk")
        if session.stopping:
            raise StopSession
        session.event("info", f"Подключаюсь к {address}. Ждём, пока пользователь примет подключение…")
        anydesk.connect(exe, address, session.password or None)
    timeout = s.connect_timeout if session.connect else 5
    window = wait_window(session, address, timeout, s.window_title)
    while not window:
        answer = session.ask("pick_window", "Окно сеанса AnyDesk не найдено автоматически. Нажмите "
                             "«Выбрать окно», затем в течение 5 секунд кликните по окну сеанса AnyDesk.")
        if answer == STOP:
            raise StopSession
        window = anydesk.pick_active_window(5)
        if not window:
            session.event("warning", "Не удалось определить окно — попробуйте ещё раз")
    session.event("info", f"Окно сеанса: «{window.title}»")
    return anydesk.WindowTarget(window)


def wait_window(session: Session, address: str, timeout: float, title_hint: str = ""):
    """Ждёт окно сеанса AnyDesk (проверка раз в 2 с), но «Стоп» прерывает ожидание сразу."""
    polls = int(timeout // 2) + 1
    for i in range(polls):
        if session.stopping:
            raise StopSession
        window = anydesk.find_session_window(address, title_hint)
        if window:
            return window
        if i + 1 < polls:
            session.sleep(2)
    return None


class SessionManager:
    """Один активный сеанс за раз: мышь и клавиатура у компьютера одни."""

    def __init__(self, db, settings_store, data_dir: Path, factory=default_factory, kb=None):
        self.db = db
        self.kb = kb
        self.store = settings_store
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.cleanup_files(self.settings().keep_files_days)
        except OSError:
            pass  # уборка не должна мешать запуску программы
        self.playbook_dirs = [self.data_dir / "playbooks", resource_dir() / "playbooks"]
        self.factory = factory
        self.alert = beep  # вызывается при каждом вопросе технику; в тестах подменяется
        self.current: Session | None = None

    def cleanup_files(self, days: int, now: float | None = None) -> int:
        """Удаляет скриншоты старше days дней (0 — хранить вечно). Возвращает число удалённых."""
        if days <= 0:
            return 0
        limit = (now or time.time()) - days * 86400
        removed = 0
        for path in self.files_dir.glob("*/*.jpg"):
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        for folder in self.files_dir.iterdir():
            try:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:
                continue  # папка занята (открыта консоль, антивирус) — удалим в следующий раз
        return removed

    def delete_files(self, ticket_id: int) -> None:
        """Скриншоты удалённого тикета: на них экран пользователя."""
        shutil.rmtree(self.files_dir / str(int(ticket_id)), ignore_errors=True)

    def settings(self):
        return self.store.load()

    def start(self, ticket_id: int, **options) -> Session:
        if self.current and self.current.alive:
            raise RuntimeError(f"Уже идёт сеанс по тикету #{self.current.tid}. Сначала остановите его.")
        ticket = self.db.get_ticket(ticket_id)
        if not ticket:
            raise KeyError(ticket_id)
        self.current = Session(self, ticket, **options)
        self.current.thread.start()
        return self.current

    def get(self, ticket_id: int) -> Session | None:
        if self.current and self.current.tid == ticket_id and self.current.alive:
            return self.current
        return None
