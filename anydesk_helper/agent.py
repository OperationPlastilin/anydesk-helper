"""Агент: проба системы -> план и вопросы -> шаги (команда / скриншот / клик) до результата.

С оператором (техником) агент общается через объект operator:
  operator.event(kind, text, **data)  — запись в ход решения тикета;
  operator.ask(kind, text, **data)    — вопрос, блокирует до ответа;
  operator.checkpoint()               — пауза/стоп/новые подсказки между шагами;
  operator.image(step, image)         — сохранить скриншот, вернуть имя файла.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import screen
from .actions import (GUI_ACTIONS, Action, ActionError, extract_json, needs_confirmation,
                      parse_action)
from .bridge import INIT, PROBE, CommandResult, enduser_dialog
from .knowledge import KINDS, looks_like_error, prompt_block
from .llm import LLMError, user_message
from .prompts import (PLAN_USER, REFLECT_SYSTEM, REFLECT_USER, STEP_USER, load_playbooks,
                      notes_block, plan_system, step_system)

YES = "yes"
NO = "no"
STOP = "stop"


class StopSession(Exception):
    pass


@dataclass
class State:
    probe: str = "unknown"
    plan: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    last: str = "(nothing yet - start with step 1 of the plan)"
    want_screenshot: bool = False
    errors: list[str] = field(default_factory=list)  # упавшие команды — материал для уроков
    last_typed: str = ""
    recent: list[tuple] = field(default_factory=list)


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
            try:
                return parse(extract_json(reply))
            except ActionError as e:
                self.op.event("warning", f"Модель ответила не по формату: {e}", reply=reply[:2000])
                if attempt == 2:
                    raise
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"Invalid reply: {e}. Answer again with ONE "
                              "JSON object exactly in the required format."}]

    def call_model(self, fn, *args):
        """Ошибки API не роняют сеанс: спрашиваем техника, повторять ли."""
        while True:
            try:
                return fn(*args)
            except LLMError as e:
                answer = self.op.ask("error", f"Ошибка обращения к модели: {e}\nПовторить запрос?")
                if answer in (STOP, NO):
                    raise StopSession from None

    # ------------------------------------------------------------ этапы

    def run(self) -> bool:
        self.op.event("info", "Открываю PowerShell на компьютере пользователя и собираю сведения о системе")
        self.prepare_console()
        self.load_experience()
        self.make_plan()
        return self.loop()

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
        self.controller.open_terminal(admin=False)
        result = self.bridge.run(f"{INIT}; {PROBE}", timeout=40)
        self.op.event("command", "Сведения о системе", command="(проба системы)",
                      output=result.output, status=result.status, exit_code=result.exit_code)
        if result.status == "done":
            self.state.probe = result.output.strip()
            self.state.last = "Console is open (not admin unless the probe says admin: True)."
        else:
            self.op.event("warning", "Консоль не ответила. Проверьте в AnyDesk: «Передавать "
                          "комбинации клавиш» и синхронизацию буфера обмена.")
            self.state.last = result.for_model()
            self.state.want_screenshot = self.s.vision

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
            notes=notes_block(st.notes), history="\n".join(st.history[-20:]) or "(empty)",
            last=st.last, step=step, max_steps=self.s.max_steps, image_note=image_note)

    def loop(self) -> bool:
        system = step_system(self.s.language, self.s.vision, self.s.coords, self.playbook,
                             self.experience_text)
        first = True
        for step in range(1, self.s.max_steps + 1):
            self.state.notes += self.op.checkpoint()
            frame = None
            if self.state.want_screenshot and self.s.vision:
                frame = self.capture()
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
            result = self.bridge.run(a.command, timeout=a.timeout)
            self.log_command(a.command, result)
            text = result.for_model()
            if result.status == "done" and looks_like_error(result.output, result.exit_code):
                st.errors.append(f"{a.command}\n  -> exit {result.exit_code}: {result.output.strip()[:400]}")
                text += self.known_fix(a.command, result.output)
            return self.remember(step, a, text)
        if a.type == "wait":
            if self.bridge.pending:
                result = self.bridge.wait(a.seconds)
                self.log_command("(ожидание завершения команды)", result)
                return self.remember(step, a, result.for_model())
            self.sleep(a.seconds)
            st.want_screenshot = self.s.vision
            return self.remember(step, a, "waited")
        if a.type == "open_terminal":
            self.bridge.pending = None
            self.controller.open_terminal(admin=a.admin)
            if a.admin:
                st.want_screenshot = self.s.vision
                return self.remember(step, a, "Admin PowerShell requested: a UAC prompt should be on "
                                     "the screen now. Confirm it (Yes/Да), then check with a command.")
            result = self.bridge.run(f"{INIT}; 'console ready'", timeout=30)
            return self.remember(step, a, result.for_model())
        if a.type == "ask_enduser":
            self.op.event("info", f"Вопрос пользователю на его экране: {a.text}")
            result = self.bridge.run(enduser_dialog(a.text), timeout=300)
            self.log_command("(диалог с пользователем)", result)
            if result.status == "done":
                answer = result.output.split("ANSWER:", 1)[-1].strip()
                st.notes.append(f"Customer was asked: {a.text} Customer answered: {answer or '(empty)'}")
                return self.remember(step, a, f"customer answered: {answer!r}")
            return self.remember(step, a, result.for_model())
        if a.type == "screenshot":
            st.want_screenshot = True
            return self.remember(step, a, "screenshot will be attached to the next message")

        # GUI-действия
        try:
            result = self.controller.execute(a, frame)
        except Exception as e:
            if type(e).__name__ == "FailSafeException":  # pyautogui: мышь увели в угол
                raise StopSession from None
            result = f"ERROR: {e}"
        if a.type == "type":
            st.last_typed = "" if a.enter else a.text
        elif a.type == "key" and a.keys == ["enter"]:
            st.last_typed = ""
        st.want_screenshot = True  # после клика надо посмотреть, что получилось
        self.sleep(self.s.step_delay)
        return self.remember(step, a, result + "; new screenshot attached")

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
        except (LLMError, ActionError) as e:
            self.op.event("warning", f"Не удалось извлечь уроки из сеанса: {e}")
            return []
        saved = []
        for x in lessons[:3]:
            kind = x.get("kind") if x.get("kind") in KINDS else "solution"
            if kind == "solution" and outcome != "solved":
                kind = "pitfall"  # «рецепт» из нерешённого тикета — не рецепт
            try:
                saved.append(self.kb.add(kind, str(x.get("title", "")), str(x.get("problem", "")),
                                         str(x.get("solution", "")), x.get("tags") or [], t.get("id")))
            except ValueError:
                continue
        if saved:
            self.op.event("learned", f"Агент запомнил уроков: {len(saved)}",
                          lessons=[{"id": x["id"], "kind": x["kind"], "title": x["title"]} for x in saved])
        return saved

    def log_command(self, command: str, result: CommandResult) -> None:
        self.op.event("command", command, command=command, output=result.output,
                      status=result.status, exit_code=result.exit_code)
        if result.status == "no_ack" and self.s.vision:
            self.state.want_screenshot = True

    def remember(self, step: int, a: Action, result: str) -> None:
        st = self.state
        st.last = f"{a.describe()}\n-> {result}"
        short = result.replace("\n", " ")
        st.history.append(f"{step}. {a.describe()[:200]} -> {short[:200]}")
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


def _points(a: Action) -> list[tuple[float, float]]:
    points = []
    if a.x is not None:
        points.append((a.x, a.y))
    if a.x2 is not None:
        points.append((a.x2, a.y2))
    return points
