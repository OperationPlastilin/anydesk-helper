"""Главный цикл: план -> (скриншот -> действие от модели -> выполнение) до завершения."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import screen
from .actions import Action, ActionError, extract_json, needs_confirmation, parse_action
from .config import Config
from .controller import Controller
from .llm import LLM, LLMError, user_message
from .prompts import (COORDS_HELP, PLAN_SYSTEM, STEP_SYSTEM, SURFACE, load_playbooks,
                      playbook_block)


class StopSession(Exception):
    pass


class Console:
    """Общение с оператором. Вынесено отдельно, чтобы в тестах подменять ввод."""

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str) -> str:
        return input(prompt).strip()


class SessionLog:
    def __init__(self, base_dir: str):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(base_dir) / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = open(self.dir / "session.jsonl", "a", encoding="utf-8")

    def event(self, kind: str, **data) -> None:
        record = {"time": datetime.now().isoformat(timespec="seconds"), "event": kind, **data}
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def image(self, step: int, frame: screen.Frame, action: Action | None) -> None:
        points = []
        if action and action.x is not None:
            points.append((action.x, action.y))
        if action and action.x2 is not None:
            points.append((action.x2, action.y2))
        screen.annotate(frame.image, points).save(self.dir / f"step_{step:03d}.jpg", quality=80)


@dataclass
class State:
    task: str
    plan: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # ответы и подсказки оператора
    history: list[str] = field(default_factory=list)
    last_typed: str = ""
    recent: list[tuple] = field(default_factory=list)


class Agent:
    def __init__(self, cfg: Config, llm: LLM, target, controller: Controller,
                 console: Console | None = None, log: SessionLog | None = None,
                 surface: str = "anydesk", grab=screen.grab):
        self.cfg = cfg
        self.llm = llm
        self.target = target
        self.controller = controller
        self.console = console or Console()
        self.log = log
        self.grab = grab
        a = cfg.agent
        self.playbook = ""
        self.fmt = {"surface": SURFACE[surface], "coords_help": COORDS_HELP[cfg.llm.coords],
                    "language": a.language}

    # ---------- скриншоты и запросы к модели ----------

    def capture(self) -> screen.Frame:
        self.target.focus()
        region = self.target.region()
        return screen.make_frame(self.grab(region), region, self.cfg.agent.screenshot_max_side)

    def ask_model(self, system: str, text: str, frame: screen.Frame, parse):
        """Запрос к модели с повтором, если ответ не разобрался (до 2 исправлений)."""
        messages = [{"role": "system", "content": system},
                    user_message(text, frame.to_data_url())]
        for attempt in range(3):
            reply = self.llm.chat(messages)
            try:
                return parse(extract_json(reply))
            except ActionError as e:
                if self.log:
                    self.log.event("bad_reply", reply=reply, error=str(e))
                if attempt == 2:
                    raise
                self.console.say(f"   (модель ответила некорректно: {e} — переспрашиваю)")
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"Your reply was invalid: {e}. "
                              "Reply with exactly one valid JSON object as described."}]

    # ---------- этап 1: план и вопросы ----------

    def make_plan(self, state: State) -> None:
        self.console.say("\n[ИИ] Смотрю на экран и составляю план...")
        frame = self.capture()
        system = PLAN_SYSTEM.format(**self.fmt, playbook=playbook_block(self.playbook))

        def parse(data: dict) -> dict:
            plan = data.get("plan")
            if not isinstance(plan, list) or not plan:
                raise ActionError("'plan' must be a non-empty list of steps")
            questions = data.get("questions") or []
            if not isinstance(questions, list):
                raise ActionError("'questions' must be a list")
            return {"understanding": str(data.get("understanding", "")),
                    "plan": [str(s) for s in plan], "questions": [str(q) for q in questions]}

        result = self.ask_model(system, f"Operator's task: {state.task}\n"
                                f"Screenshot size: {frame.size[0]}x{frame.size[1]}.", frame, parse)
        state.plan = result["plan"]
        if self.log:
            self.log.event("plan", **result)
        if result["understanding"]:
            self.console.say(f"[ИИ] Понял задачу так: {result['understanding']}")
        for q in result["questions"]:
            answer = self.console.ask(f"[ИИ спрашивает] {q}\n  > ")
            state.notes.append(f"Q: {q} A: {answer or '(no answer)'}")
        self.show_plan(state)
        fix = self.console.ask("Enter — начать, или напишите поправку к плану: ")
        if fix:
            state.notes.append(f"Operator's correction to the plan: {fix}")

    def show_plan(self, state: State) -> None:
        self.console.say("[ИИ] План:")
        for i, step in enumerate(state.plan, 1):
            self.console.say(f"   {i}. {step}")

    # ---------- этап 2: пошаговое выполнение ----------

    def step_prompt(self, state: State, step: int, frame: screen.Frame) -> str:
        lines = [f"TASK: {state.task}", "PLAN:"]
        lines += [f"{i}. {s}" for i, s in enumerate(state.plan, 1)]
        if state.notes:
            lines.append("OPERATOR NOTES (answers and hints, follow them):")
            lines += [f"- {n}" for n in state.notes]
        keep = self.cfg.agent.history
        if state.history:
            lines.append("YOUR PREVIOUS ACTIONS (oldest first):")
            lines += state.history[-keep:]
        w, h = frame.size
        lines.append(f"STEP {step}/{self.cfg.agent.max_steps}. Screenshot {w}x{h} px. "
                     "Reply with one JSON action.")
        return "\n".join(lines)

    def next_action(self, state: State, step: int, frame: screen.Frame) -> Action:
        system = STEP_SYSTEM.format(**self.fmt, playbook=playbook_block(self.playbook))
        w, h = frame.size
        return self.ask_model(system, self.step_prompt(state, step, frame), frame,
                              lambda d: parse_action(d, w, h, self.cfg.llm.coords))

    def confirm(self, action: Action) -> tuple[bool, str]:
        """Возвращает (выполнять ли, подсказка оператора)."""
        answer = self.console.ask(
            "   Выполнить? [Enter/y — да, n — пропустить, q — стоп, или напишите подсказку]: ")
        low = answer.lower()
        if low in ("", "y", "д", "да", "yes"):
            return True, ""
        if low in ("q", "stop", "стоп"):
            raise StopSession
        if low in ("n", "н", "нет", "no"):
            return False, ""
        return False, answer

    def pause_menu(self, state: State) -> None:
        self.console.say("\n[Пауза]")
        while True:
            choice = self.console.ask("c — продолжить, h — подсказать агенту, p — показать план, "
                                      "q — завершить: ").lower()
            if choice in ("c", "с", ""):
                return
            if choice in ("q", "й"):
                raise StopSession
            if choice == "p":
                self.show_plan(state)
            if choice in ("h", "р"):
                hint = self.console.ask("Подсказка: ")
                if hint:
                    state.notes.append(hint)
                return

    def remember(self, state: State, step: int, text: str) -> None:
        state.history.append(f"{step}. {text}")
        if self.log:
            self.log.event("history", text=text)

    def run(self, task: str) -> bool:
        state = State(task=task)
        self.playbook = load_playbooks(self.cfg.agent.playbooks_dir, task)
        if self.log:
            self.log.event("start", task=task, model=self.cfg.llm.model)
        try:
            self.make_plan(state)
            return self.loop(state)
        except (StopSession, KeyboardInterrupt):
            self.console.say("\n[Сеанс остановлен оператором]")
            return False
        finally:
            if self.log:
                self.console.say(f"Лог и скриншоты: {self.log.dir}")

    def loop(self, state: State) -> bool:
        a_cfg = self.cfg.agent
        step = 0
        while step < a_cfg.max_steps:
            try:
                step += 1
                frame = self.capture()
                self.console.say(f"\n[Шаг {step}] думаю...")
                try:
                    action = self.next_action(state, step, frame)
                except ActionError as e:
                    self.remember(state, step, f"(your reply was invalid: {e})")
                    continue
                if self.log:
                    self.log.image(step, frame, action)
                    self.log.event("action", step=step, action=action.__dict__)
                if action.thought:
                    self.console.say(f"   {action.thought}")
                self.console.say(f"   -> {action.describe()}" + ("  [!]" if action.risky else ""))

                if action.type == "ask_user":
                    answer = self.console.ask(f"[ИИ спрашивает] {action.text}\n  > ")
                    state.notes.append(f"Q: {action.text} A: {answer or '(no answer)'}")
                    self.remember(state, step, f"asked operator: {action.text!r} -> {answer!r}")
                    continue
                if action.type == "done":
                    self.console.say(f"[ИИ] Готово: {action.text}")
                    if a_cfg.confirm == "none":
                        return True
                    verdict = self.console.ask("Задача решена? [Enter/y — да, "
                                               "или напишите, что не так]: ")
                    if verdict.lower() in ("", "y", "д", "да", "yes"):
                        return True
                    state.notes.append(f"Operator says the task is NOT done yet: {verdict}")
                    self.remember(state, step, "done -> operator rejected: " + verdict)
                    continue
                if action.type == "fail":
                    self.console.say(f"[ИИ] Не получается: {action.text}")
                    hint = self.console.ask("Напишите подсказку, чтобы продолжить, "
                                            "или Enter — завершить: ")
                    if not hint:
                        return False
                    state.notes.append(hint)
                    self.remember(state, step, f"fail -> operator hint: {hint}")
                    continue

                if needs_confirmation(action, a_cfg.confirm, state.last_typed):
                    ok, hint = self.confirm(action)
                    if not ok:
                        if hint:
                            state.notes.append(hint)
                        self.remember(state, step, f"{action.describe()} -> REJECTED by operator"
                                      + (f", hint: {hint}" if hint else ""))
                        continue

                try:
                    result = self.controller.execute(action, frame)
                except Exception as e:
                    if type(e).__name__ == "FailSafeException":  # pyautogui: мышь в углу
                        raise StopSession from None
                    result = f"ERROR: {e}"
                if action.type == "type":
                    state.last_typed = "" if action.enter else action.text
                elif action.type == "key" and action.keys == ["enter"]:
                    state.last_typed = ""
                self.remember(state, step, f"{action.describe()} -> {result}")
                self.check_loop(state, step, action)
                time.sleep(a_cfg.step_delay)
            except KeyboardInterrupt:
                self.pause_menu(state)
            except LLMError as e:
                self.console.say(f"[Ошибка модели] {e}")
                if self.console.ask("Повторить? [Enter — да, q — стоп]: ").lower() == "q":
                    return False
        self.console.say(f"[ИИ] Достигнут лимит шагов ({a_cfg.max_steps}).")
        return False

    def check_loop(self, state: State, step: int, action: Action) -> None:
        """Слабые модели часто повторяют одно и то же действие — подсказываем им."""
        state.recent = (state.recent + [action.signature()])[-3:]
        if len(state.recent) == 3 and len(set(state.recent)) == 1:
            self.remember(state, step, "NOTE: you repeated the same action 3 times and it does "
                          "not help. Choose a different approach or ask_user.")
            state.recent.clear()
