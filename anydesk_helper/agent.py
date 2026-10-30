"""Агент: проба системы -> план и вопросы -> шаги (команда / скриншот / клик) до результата.

С оператором (техником) агент общается через объект operator:
  operator.event(kind, text, **data)  — запись в ход решения тикета;
  operator.ask(kind, text, **data)    — вопрос, блокирует до ответа;
  operator.checkpoint()               — пауза/стоп/новые подсказки между шагами;
  operator.image(step, image)         — сохранить скриншот, вернуть имя файла.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import screen
from .actions import (GUI_ACTIONS, Action, ActionError, extract_json, is_remote_code,
                      needs_confirmation, parse_action)
from .anydesk import FocusError
from .bridge import INIT, PROBE, CommandResult, enduser_dialog
from .knowledge import KINDS, looks_like_error, prompt_block, scrub
from .llm import LLMError, user_message
from .prompts import (PLAN_USER, REFLECT_SYSTEM, REFLECT_USER, STEP_USER, customer_block,
                      load_playbooks, notes_block, plan_system, step_system)

YES = "yes"
NO = "no"
STOP = "stop"
WAIT_CHUNK = 60.0  # долгий wait идёт частями: между ними пауза, «Стоп» и подсказки техника
ADMIN_CHECK = ("([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
               ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")


CONSOLE_ATTEMPTS = 3  # проба консоли: первая попытка + две после ручного открытия PowerShell техником

NO_ACK_HELP = (
    "Консоль пользователя не ответила на команду. Посмотрите скриншот ниже и проверьте:\n"
    "1) открылось ли PowerShell (нет — в AnyDesk на ЭТОМ ПК: Настройки → Ввод → «Передавать комбинации "
    "клавиш», иначе Win+R открывает «Выполнить» у вас);\n"
    "2) есть ли в окне PowerShell вставленная строка (нет — включите синхронизацию буфера обмена: "
    "Настройки → Безопасность/Разрешения → «Буфер обмена» на обеих сторонах);\n"
    "3) строка есть, но не выполнилась — пусть пользователь не трогает клавиатуру и окно консоли.\n"
    "Можно открыть PowerShell у пользователя вручную и нажать «Повторить» в вопросе ниже.")


class StopSession(Exception):
    pass


def _is_failsafe(e: BaseException) -> bool:
    """pyautogui.FailSafeException (мышь увели в угол экрана) — без импорта pyautogui."""
    return type(e).__name__ == "FailSafeException"


@dataclass
class State:
    probe: str = "unknown"
    plan: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # ответы и подсказки техника
    customer: list[str] = field(default_factory=list)  # ответы пользователя — сведения, не указания
    history: list[str] = field(default_factory=list)
    last: str = "(nothing yet - start with step 1 of the plan)"
    want_screenshot: bool = False
    errors: list[str] = field(default_factory=list)  # упавшие команды — материал для уроков
    last_typed: str = ""
    recent: list[tuple] = field(default_factory=list)
    running: tuple[str, str] = ("", "")  # (id, команда) долгой команды: результат придёт через wait


class Agent:
    def __init__(self, settings, llm, target, controller, bridge, operator, ticket: dict,
                 playbook_dirs=(), grab=screen.grab, sleep=time.sleep, kb=None):
        self.s = settings
        self.llm = llm
        self.target = target
        self.controller = controller
        self.bridge = bridge
        self.op = operator
        self.ticket = ticket
        self.grab = grab
        self.sleep = sleep
        task = f"{ticket.get('title', '')}\n{ticket.get('description', '')}"
        self.playbook = load_playbooks(playbook_dirs, task)
        self.state = State()
        self.kb = kb  # база опыта (knowledge.KnowledgeBase) или None
        self.experience: list[dict] = []
        self.experience_text = ""

    # ------------------------------------------------------------ общение с моделью

    def capture(self) -> screen.Frame:
        self.target.focus()
        region = self.target.region()
        return screen.make_frame(self.grab(region), region, self.s.screenshot_max_side)

    def ask_model(self, system: str, text: str, parse, frame: screen.Frame | None = None):
        """Запрос к модели; если ответ не в формате — объясняем ошибку и переспрашиваем."""
        messages = [{"role": "system", "content": system},
                    user_message(text, frame.to_data_url() if frame else None)]
        for attempt in range(3):
            reply = self.llm.chat(messages)
            cut = getattr(self.llm, "finish_reason", "") == "length"
            try:
                if cut:  # обрезанный ответ мог кончиться черновым JSON из рассуждения — не выполняем
                    raise ActionError("Reply was cut off by max_tokens; answer briefly with the JSON object")
                return parse(extract_json(reply))
            except ActionError as e:
                self.op.event("warning", f"Модель ответила не по формату: {e}"
                              + (" (ответ обрезан по max_tokens)" if cut else ""), reply=reply[:2000])
                if attempt == 2:
                    raise
                if self.stopping:  # «Стоп» — не тратим ещё запросы на переспрашивание
                    raise StopSession from None
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"Invalid reply: {e}. Answer again with ONE "
                              "JSON object exactly in the required format."}]

    @property
    def stopping(self) -> bool:
        return bool(getattr(self.op, "stopping", False))

    def call_model(self, fn, *args):
        """Ошибки API не роняют сеанс: спрашиваем техника, повторять ли."""
        while True:
            try:
                return fn(*args)
            except LLMError as e:
                if self.stopping:  # запрос прерван «Стопом» — повторять нечего
                    raise StopSession from None
                answer = self.op.ask("error", f"Ошибка обращения к модели: {e}\nПовторить запрос?")
                if answer in (STOP, NO):
                    raise StopSession from None

    def remote(self, fn, *args, esc_before_retry: bool = False, **kw):
        """Мышь, клавиатура или консоль пользователя. Окно AnyDesk не вышло на передний план —
        вслепую не печатаем: спрашиваем техника и повторяем то же действие.
        esc_before_retry — окно «Выполнить» (open_terminal) могло остаться открытым с вписанным
        «powershell»: перед повтором закрываем его Esc, иначе получится «powershellpowershell».
        Строку в консоли после такого сбоя очищает сам Controller.paste_line."""
        retry = False
        while True:
            try:
                if retry and esc_before_retry:
                    self.controller.hotkey("esc")
                return fn(*args, **kw)
            except FocusError as e:
                self.op.event("warning", f"Окно AnyDesk не на переднем плане: {e}")
                answer = self.op.ask("error", "Не удалось вывести окно AnyDesk на передний план. "
                                     "Разверните окно сеанса AnyDesk. Повторить?")
                if answer in (STOP, NO):
                    raise StopSession from None
                retry = True

    def console(self, command: str, timeout: float) -> CommandResult:
        return self.check_refused(self.remote(self.bridge.run, command, timeout=timeout))

    def check_refused(self, result: CommandResult) -> CommandResult:
        """Обёртка увидела, что консоль — на компьютере техника: дальше работать нельзя."""
        if result.status == "refused":
            self.bridge.pending = None  # и Ctrl+C туда не шлём
            self.op.event("error", "Команда попала в консоль на компьютере техника — сеанс остановлен")
            raise StopSession
        return result

    # ------------------------------------------------------------ этапы

    def run(self, resume: dict | None = None) -> bool:
        """resume — сохранённые план, ответы и история прерванного сеанса (restore_state)."""
        try:
            self.op.event("info", "Открываю PowerShell на компьютере пользователя и собираю сведения о системе")
            self.prepare_console()
            self.state.notes += self.op.checkpoint()  # «Стоп» во время пробы — план не запрашиваем
            self.load_experience()
            if resume and resume.get("plan"):
                self.resume(resume)
            else:
                self.make_plan()
            return self.loop()
        except StopSession:
            self.cancel_command()
            raise
        except Exception as e:
            if not _is_failsafe(e):
                raise
            # Аварийная остановка: Ctrl+C не шлём — техник забрал мышь и клавиатуру у агента.
            self.op.event("warning", "Аварийная остановка: мышь в углу экрана")
            raise StopSession from None

    def resume(self, saved: dict) -> None:
        st = self.state
        st.plan = list(saved["plan"])
        st.notes += saved.get("notes", [])
        st.history = list(saved.get("history", []))
        st.last = ("The session was interrupted and has been restarted; the console was reopened. "
                   "First check with a command what is already done, then continue the plan.")
        self.op.event("plan", "Продолжаю по сохранённому плану", plan=st.plan, questions=[])
        # Сохранённый план мог быть так и не одобрен (Стоп или перезапуск на вопросе о плане).
        answer = self.op.ask("plan", "Сеанс был прерван. Продолжить по сохранённому плану?", plan=st.plan)
        self.check_stop(answer)
        if answer not in ("", YES):
            st.notes.append(f"Technician's correction to the plan: {answer}")
            self.make_plan()

    def cancel_command(self) -> None:
        """Стоп во время долгой команды — прерываем её у пользователя (Ctrl+C в консоли)."""
        if not self.bridge.pending:
            return
        try:
            self.bridge.cancel()
            self.op.event("info", "Выполнявшаяся команда прервана (Ctrl+C в консоли пользователя)")
        except Exception as e:  # уборка не должна подменять остановку (окно не в фокусе, мышь в углу)
            self.bridge.pending = None
            self.op.event("warning", f"Не удалось прервать команду в консоли пользователя: {e}")

    def load_experience(self) -> None:
        if not self.kb:
            return
        t = self.ticket
        self.experience = self.kb.search(f"{t.get('title', '')} {t.get('description', '')}",
                                         limit=self.s.experience_limit)
        if not self.experience:
            return
        self.experience_text = prompt_block(self.experience)
        self.kb.mark_used(t.get("id", 0), [x["id"] for x in self.experience])
        self.op.event("experience", f"Из базы опыта взято уроков: {len(self.experience)}",
                      lessons=[{"id": x["id"], "kind": x["kind"], "title": x["title"]} for x in self.experience])

    def prepare_console(self) -> None:
        self.remote(self.controller.open_terminal, admin=False, esc_before_retry=True)
        for attempt in range(1, CONSOLE_ATTEMPTS + 1):
            result = self.console(f"{INIT}; {PROBE}", 40)
            self.op.event("command", "Сведения о системе", command="(проба системы)",
                          output=result.output, status=result.status, exit_code=result.exit_code)
            if result.status == "done":
                self.state.probe = result.output.strip()
                self.state.last = "Console is open (not admin unless the probe says admin: True)."
                return
            # Скриншот — технику для диагностики (без запроса к модели): видно, открылось ли «Выполнить»,
            # PowerShell, есть ли в нём вставленная строка.
            self.op.event("warning", NO_ACK_HELP, image=self.diagnostic_shot())
            if attempt == CONSOLE_ATTEMPTS:
                break
            # Без консоли агент работает вслепую — даём технику открыть PowerShell у пользователя
            # руками (так работает, даже если AnyDesk не передаёт Win+R) и повторить пробу.
            answer = self.op.ask("error", "Консоль пользователя не ответила (скриншот выше). Откройте у "
                                 "пользователя PowerShell вручную (Win+R → powershell → Enter), щёлкните в "
                                 "его окно и нажмите «Повторить». «Стоп» — завершить сеанс.",
                                 skip_label="Продолжить без консоли")
            self.check_stop(answer)
            if answer == NO:
                break  # продолжаем без консоли: модель попробует разобраться по скриншоту
        self.state.last = result.for_model()
        self.state.want_screenshot = self.s.vision

    def diagnostic_shot(self) -> str | None:
        try:
            return self.op.image(0, self.capture().image)
        except Exception:  # скриншот — подсказка, сеанс из-за него не падает
            return None

    def make_plan(self) -> None:
        st, t = self.state, self.ticket
        system = plan_system(self.s.language, self.playbook, self.experience_text)

        def parse(data: dict) -> dict:
            plan = data.get("plan")
            if not isinstance(plan, list) or not plan:
                raise ActionError("'plan' must be a non-empty list of steps")
            questions = data.get("questions") or []
            if not isinstance(questions, list):
                raise ActionError("'questions' must be a list")
            return {"summary": str(data.get("summary", "")),
                    "plan": [str(p) for p in plan], "questions": [str(q) for q in questions]}

        while True:
            user = PLAN_USER.format(ticket_id=t.get("id", ""), title=t.get("title", ""),
                                    description=t.get("description", ""), probe=st.probe,
                                    notes=notes_block(st.notes))
            self.op.event("prompt", "Промт для составления плана", system=system, user=user)
            self.op.event("info", "ИИ составляет план…")
            result = self.call_model(self.ask_model, system, user, parse)
            hints = self.op.checkpoint()  # «Стоп»/пауза/подсказка, пока модель думала
            if hints:
                st.notes += hints  # план составлен без подсказки — составляем заново
                continue
            st.plan = result["plan"]
            self.op.event("plan", result["summary"], plan=st.plan, questions=result["questions"])
            for q in result["questions"]:
                answer = self.op.ask("question", q)
                self.check_stop(answer)
                st.notes.append(f"Q: {q} A: {answer or '(no answer)'}")
            answer = self.op.ask("plan", "Проверьте план. Начать выполнение?", plan=st.plan)
            self.check_stop(answer)
            if answer in ("", YES):
                return
            # Техник поправил план — учитываем и составляем заново.
            st.notes.append(f"Technician's correction to the plan: {answer}")

    def step_text(self, step: int, frame: screen.Frame | None) -> str:
        st, t = self.state, self.ticket
        image_note = ""
        if frame:
            w, h = frame.size
            image_note = f" A screenshot of the customer's screen is attached ({w}x{h} px)."
        return STEP_USER.format(
            ticket_id=t.get("id", ""), title=t.get("title", ""), description=t.get("description", ""),
            probe=st.probe, plan="\n".join(f"{i}. {p}" for i, p in enumerate(st.plan, 1)),
            notes=notes_block(st.notes), customer=customer_block(st.customer),
            history="\n".join(st.history[-20:]) or "(empty)",
            last=st.last, step=step, max_steps=self.s.max_steps, image_note=image_note)

    def loop(self) -> bool:
        system = step_system(self.s.language, self.s.vision, self.s.coords, self.playbook,
                             self.experience_text)
        first = True
        for step in range(1, self.s.max_steps + 1):
            self.state.notes += self.op.checkpoint()
            frame = None
            if self.state.want_screenshot and self.s.vision:
                frame = self.remote(self.capture)
                self.state.want_screenshot = False
            text = self.step_text(step, frame)
            if first:
                self.op.event("prompt", "Промт для шагов (первый шаг)", system=system, user=text)
                first = False
            w, h = frame.size if frame else (0, 0)
            try:
                action = self.call_model(self.ask_model, system, text,
                                         lambda d: parse_action(d, w, h, self.s.coords), frame)
            except ActionError as e:
                self.state.last = f"Your previous reply was invalid: {e}"
                continue
            # «Стоп», нажатый, пока модель думала, не даёт выполнить её действие (checkpoint
            # бросает StopSession); подсказка из этого времени — повод решить шаг заново.
            hints = self.op.checkpoint()
            if hints:
                self.state.notes += hints
                self.state.last += ("\nNOTE: the technician sent new hints while you were thinking. "
                                    "Take them into account and choose the action again.")
                self.state.want_screenshot = self.state.want_screenshot or frame is not None
                continue
            if action.type in GUI_ACTIONS and not self.s.vision:
                self.state.last = "Screen actions are not available. Use console commands or ask_operator."
                continue
            image = self.op.image(step, screen.annotate(frame.image, _points(action))) if frame else None
            self.op.event("step", action.thought, step=step, observation=action.observation,
                          plan_step=action.plan_step, action=action.type,
                          description=action.describe(), risky=action.risky, image=image)
            finished = self.handle(action, step, frame)
            if finished is not None:
                return finished
        self.op.event("warning", f"Достигнут лимит шагов ({self.s.max_steps})")
        return False

    # ------------------------------------------------------------ действия

    def handle(self, a: Action, step: int, frame) -> bool | None:
        """Выполняет действие. True/False — сеанс закончен (успех/нет), None — продолжаем."""
        st = self.state
        if a.type == "ask_operator":
            answer = self.op.ask("question", a.text)
            self.check_stop(answer)
            st.notes.append(f"Q: {a.text} A: {answer or '(no answer)'}")
            return self.remember(step, a, f"technician answered: {answer!r}")
        if a.type == "done":
            if self.s.confirm == "none":
                self.op.event("done", a.text)
                return True
            answer = self.op.ask("done", f"ИИ считает задачу решённой: {a.text}\nПодтверждаете?")
            self.check_stop(answer)
            if answer in ("", YES):
                self.op.event("done", a.text)
                return True
            st.notes.append(f"Technician says the task is NOT done yet: {answer}")
            return self.remember(step, a, f"technician rejected: {answer}")
        if a.type == "fail":
            answer = self.op.ask("fail", f"ИИ не может решить задачу: {a.text}\n"
                                 "Дайте подсказку, чтобы продолжить, или завершите сеанс.")
            if answer in ("", STOP, NO):
                self.op.event("failed", a.text)
                return False
            st.notes.append(answer)
            return self.remember(step, a, f"technician hint: {answer}")

        if needs_confirmation(a, self.s.confirm, st.last_typed):
            answer = self.op.ask("confirm", a.describe(), action=a.type, risky=a.risky,
                                 command=a.command)
            self.check_stop(answer)
            if answer != YES:
                hint = "" if answer == NO else answer
                if hint:
                    st.notes.append(hint)
                return self.remember(step, a, "REJECTED by technician" + (f", hint: {hint}" if hint else ""))

        if a.type == "run":
            result = self.console(a.command, a.timeout or self.s.command_timeout)
            if result.status == "busy":  # прошлая команда ещё идёт — эта не отправлена
                self.op.event("warning", "Команда не отправлена: предыдущая ещё выполняется")
                return self.remember(step, a, result.for_model())
            self.log_command(a.command, result)
            if self.bridge.pending:
                st.running = (self.bridge.pending, a.command)
            return self.remember(step, a, result.for_model() + self.check_failure(a.command, result))
        if a.type == "wait":
            if self.bridge.pending:
                cid = self.bridge.pending
                result, waited, hints = self.wait_parts(
                    a.seconds, lambda t: self.check_refused(self.bridge.wait(t)))
                self.log_command("(ожидание завершения команды)", result)
                text = result.for_model()
                if st.running[0] == cid:  # долгая команда закончилась — разбираем её ошибку тоже
                    text += self.check_failure(st.running[1], result)
                text += self.hints_note(hints, waited)
                # Ждать установщик много раз подряд — нормально, это не зацикливание.
                return self.remember(step, a, text, track=False)
            _, waited, hints = self.wait_parts(a.seconds, self.sleep)
            st.want_screenshot = self.s.vision
            return self.remember(step, a, "waited" + self.hints_note(hints, waited))
        if a.type == "open_terminal":
            self.bridge.pending = None
            self.remote(self.controller.open_terminal, admin=a.admin, esc_before_retry=True)
            if a.admin:
                st.want_screenshot = self.s.vision
                # «admin: False» из пробы относится к старой консоли — модель не должна на него опираться.
                st.probe = re.sub(r"admin: \w+", "admin: unknown (new console - check it)", st.probe)
                return self.remember(step, a, "Admin PowerShell requested: a UAC prompt should be on "
                                     "the screen now. Confirm it (Yes/Да), then check that the new "
                                     f"console is admin with this exact command: {ADMIN_CHECK}")
            result = self.console(f"{INIT}; 'console ready'", 30)
            return self.remember(step, a, result.for_model())
        if a.type == "ask_enduser":
            if not self.bridge.pending:  # иначе диалог не отправится (busy)
                self.op.event("info", f"Вопрос пользователю на его экране: {a.text}")
            result = self.console(enduser_dialog(a.text), 300)
            if result.status == "busy":
                self.op.event("warning", "Вопрос пользователю не показан: предыдущая команда ещё выполняется")
                return self.remember(step, a, result.for_model())
            self.log_command("(диалог с пользователем)", result)
            if result.status != "done":
                return self.remember(step, a, result.for_model())
            answer = enduser_answer(result.output)
            if answer is None:  # скрипт диалога упал (Add-Type, нет рабочего стола…) — это не ответ
                return self.remember(step, a, "dialog failed: " + result.for_model())
            # Не в notes: там подсказки техника, а ответ пользователя — непроверенный текст.
            st.customer.append(f"Q: {a.text} A: {answer or '(empty)'}")
            return self.remember(step, a, f"customer answered: {answer!r}")
        if a.type == "screenshot":
            st.want_screenshot = True
            return self.remember(step, a, "screenshot will be attached to the next message")

        # GUI-действия
        try:
            result = self.remote(self.controller.execute, a, frame)
        except StopSession:
            raise
        except Exception as e:
            if _is_failsafe(e):  # pyautogui: мышь увели в угол — аварийная остановка в run()
                raise
            result = f"ERROR: {e}"
        if a.type == "type":
            st.last_typed = "" if a.enter else a.text
        elif a.type == "key" and a.keys == ["enter"]:
            st.last_typed = ""
        st.want_screenshot = True  # после клика надо посмотреть, что получилось
        self.sleep(self.s.step_delay)
        return self.remember(step, a, result + "; new screenshot attached")

    def wait_parts(self, seconds: float, wait_part) -> tuple:
        """Ждёт частями по WAIT_CHUNK, между частями — checkpoint(): пауза, «Стоп» и подсказки
        техника не ждут конца 15-минутного wait. Подсказка прерывает ожидание (запросов к модели
        это не добавляет). Возвращает (результат последней части, сколько ждали, подсказки)."""
        waited = 0.0
        while True:
            part = min(WAIT_CHUNK, seconds - waited)
            result = wait_part(part)
            waited += part
            if waited >= seconds or (isinstance(result, CommandResult) and result.status != "running"):
                return result, waited, []
            hints = self.op.checkpoint()
            if hints:
                return result, waited, hints

    def hints_note(self, hints: list[str], waited: float) -> str:
        if not hints:
            return ""
        self.state.notes += hints
        return (f"\nNOTE: stopped waiting after {waited:g} s because the technician sent new hints. "
                "Take them into account.")

    def check_failure(self, command: str, result: CommandResult) -> str:
        """Команда упала (сразу или после wait): запоминаем для уроков и ищем подсказку в опыте."""
        if result.status != "done" or not looks_like_error(result.output, result.exit_code):
            return ""
        self.state.errors.append(f"{command}\n  -> exit {result.exit_code}: {result.output.strip()[:400]}")
        return self.known_fix(command, result.output)

    def known_fix(self, command: str, output: str) -> str:
        """Команда упала — может, мы уже встречали такую ошибку?"""
        if not self.kb:
            return ""
        used = {x["id"] for x in self.experience}
        hits = self.kb.search(f"{command} {output[:800]}", limit=2, min_score=0.2, exclude=used)
        if not hits:
            return ""
        self.experience += hits
        self.kb.mark_used(self.ticket.get("id", 0), [h["id"] for h in hits])
        self.op.event("experience", "Похожая ошибка уже встречалась — подсказка из базы опыта",
                      lessons=[{"id": h["id"], "kind": h["kind"], "title": h["title"]} for h in hits])
        return "\n\nKNOWN FROM EXPERIENCE:\n" + prompt_block(hits)

    def learn(self, outcome: str) -> list[dict]:
        """Рефлексия после сеанса: модель формулирует уроки, они попадают в базу опыта."""
        st, t = self.state, self.ticket
        if not self.kb or not st.history:
            return []
        user = REFLECT_USER.format(
            title=t.get("title", ""), description=t.get("description", ""), outcome=outcome,
            probe=st.probe, plan="\n".join(f"{i}. {p}" for i, p in enumerate(st.plan, 1)),
            notes=notes_block(st.notes), history="\n".join(st.history[-40:]),
            errors="\n".join(st.errors[-10:]) or "(none)")

        def parse(data: dict) -> list[dict]:
            lessons = data.get("lessons")
            if not isinstance(lessons, list):
                raise ActionError("'lessons' must be a list")
            return [x for x in lessons if isinstance(x, dict) and x.get("title")]

        try:
            lessons = self.ask_model(REFLECT_SYSTEM.format(language=self.s.language), user, parse)
        except StopSession:
            return []
        except (LLMError, ActionError) as e:
            self.op.event("warning", f"Не удалось извлечь уроки из сеанса: {e}")
            return []
        saved, held = [], []
        for x in lessons[:3]:
            kind = x.get("kind") if x.get("kind") in KINDS else "solution"
            if kind == "solution" and outcome != "solved":
                kind = "pitfall"  # «рецепт» из нерешённого тикета — не рецепт
            solution = str(x.get("solution", ""))
            try:
                lesson = self.kb.add(kind, str(x.get("title", "")), str(x.get("problem", "")),
                                     solution, x.get("tags") or [], t.get("id"))
            except ValueError:
                continue
            # Ссылка или загрузка кода из сети могла прийти из ответа пользователя или вывода
            # команды. Такой урок (или его новое решение) не попадёт в промты, пока техник
            # не проверит и не включит его в «Базе опыта».
            if (lesson.get("enabled", True) and lesson["solution"] == scrub(solution).strip()
                    and is_remote_code(f"{lesson['title']}\n{lesson['problem']}\n{lesson['solution']}")):
                lesson = self.kb.update(lesson["id"], enabled=False) or lesson
                held.append(lesson["title"])
            saved.append(lesson)
        if saved:
            self.op.event("learned", f"Агент запомнил уроков: {len(saved)}",
                          lessons=[{"id": x["id"], "kind": x["kind"], "title": x["title"]} for x in saved])
        if held:
            self.op.event("warning", "Уроки отключены до проверки (в решении ссылка или загрузка кода "
                          "из сети) — проверьте и включите их в «Базе опыта»: " + "; ".join(held))
        return saved

    def log_command(self, command: str, result: CommandResult) -> None:
        self.op.event("command", command, command=command, output=result.output,
                      status=result.status, exit_code=result.exit_code)
        if result.status == "no_ack" and self.s.vision:
            self.state.want_screenshot = True

    def remember(self, step: int, a: Action, result: str, track: bool = True) -> None:
        """track=False — действие не считается для поиска зацикливания (wait на идущую команду)."""
        st = self.state
        st.last = f"{a.describe()}\n-> {result}"
        short = result.replace("\n", " ")
        st.history.append(f"{step}. {a.describe()[:200]} -> {short[:200]}")
        if not track:
            return None
        st.recent = (st.recent + [a.signature()])[-3:]
        if len(st.recent) == 3 and len(set(st.recent)) == 1:
            st.last += ("\nNOTE: you repeated the same action 3 times without progress. "
                        "Choose a different approach or ask_operator.")
            st.recent.clear()
        return None

    @staticmethod
    def check_stop(answer: str) -> None:
        if answer == STOP:
            raise StopSession


def restore_state(events: list[dict]) -> dict:
    """Восстанавливает из ленты тикета план, ответы техника и историю шагов —
    чтобы продолжить сеанс после перезапуска программы, а не начинать заново."""
    plan, notes, history, questions = [], [], [], {}
    attach = False  # к шагу относится только первая команда после него (не проба нового сеанса)
    for e in events:
        d, kind = e.get("data") or {}, e["kind"]
        if kind == "plan" and d.get("plan"):
            plan = d["plan"]
        elif kind == "ask":
            questions[d.get("ask_id")] = (d.get("ask_kind"), e["text"])
        elif kind == "answer":
            ask_kind, text = questions.get(d.get("ask_id"), (d.get("ask_kind"), ""))
            value = d.get("value", "")
            if ask_kind == "question":
                notes.append(f"Q: {text} A: {value or '(no answer)'}")
            elif ask_kind in ("plan", "done", "fail", "confirm") and value not in ("", YES, NO, STOP):
                notes.append(value)
        elif kind == "hint":
            notes.append(e["text"])
        elif kind == "step":
            history.append(f"{d.get('step')}. {d.get('description', '')[:200]}")
            attach = True
        elif kind == "command" and attach and d.get("command") != "(проба системы)":
            out = (d.get("output") or "").strip().replace("\n", " ")
            history[-1] += f" -> {d.get('status')}, exit {d.get('exit_code')}: {out[:150]}"
            attach = False
    return {"plan": plan, "notes": notes, "history": history[-20:]}


def enduser_answer(output: str) -> str | None:
    """Ответ из диалога ask_enduser: скрипт печатает последней строкой «ANSWER: …» (поле ввода
    однострочное). Нет маркера в начале строки или перед ним есть вывод (ошибки Add-Type,
    ShowDialog…) — диалог не сработал: None."""
    marks = list(re.finditer(r"(?m)^ANSWER:", output))
    if not marks or output[:marks[-1].start()].strip():
        return None
    return output[marks[-1].end():].strip()


def _points(a: Action) -> list[tuple[float, float]]:
    points = []
    if a.x is not None:
        points.append((a.x, a.y))
    if a.x2 is not None:
        points.append((a.x2, a.y2))
    return points
