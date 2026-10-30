from anydesk_helper.agent import NO, STOP, YES, Agent
from anydesk_helper.bridge import ConsoleBridge
from anydesk_helper.config import Settings

from .fakes import Clock, FakeClipboard, FakeLLM, FakeOperator, FakeRemote, FakeTarget, fake_grab

PLAN = {"summary": "поставить Office", "questions": ["Какая лицензия?"],
        "plan": ["Проверить Office", "Установить", "Проверить"]}


def make_agent(replies, answers, outputs=None, vision=True, confirm="risky", **remote_kw):
    s = Settings(vision=vision, confirm=confirm, step_delay=0)
    clip = FakeClipboard()
    remote = FakeRemote(clip, outputs or {}, **remote_kw)
    clock = Clock()
    bridge = ConsoleBridge(remote, FakeTarget(), clipboard=clip, sleep=clock.sleep, clock=clock)
    llm = FakeLLM(replies)
    op = FakeOperator(answers)
    ticket = {"id": 7, "title": "Установить Microsoft Office", "description": "Word и Excel"}
    agent = Agent(s, llm, FakeTarget(), remote, bridge, op, ticket, grab=fake_grab, sleep=lambda s: None)
    return agent, llm, op, remote


def test_console_first_session():
    replies = [
        PLAN,
        "это не JSON",  # модель ошиблась — агент объяснит и переспросит
        {"observation": "консоль готова", "plan_step": 1, "thought": "проверю",
         "action": "run", "command": "Get-Package *Office*", "risky": False},
        {"observation": "Office нет", "plan_step": 2, "thought": "ставлю", "action": "run",
         "command": "winget install Microsoft.Office -e", "timeout": 1800, "risky": True},
        {"observation": "готово", "plan_step": 3, "thought": "спрошу логин", "action": "ask_operator",
         "question": "Логин учётки?"},
        {"observation": "всё ок", "plan_step": 3, "thought": "", "action": "done", "summary": "Office установлен"},
    ]
    outputs = {"COMPUTERNAME": "PC: BUH-01; admin: True", "Get-Package": "", "winget": "Successfully installed"}
    # ответы техника: лицензия, план ок, подтверждение установки, логин, «решено»
    agent, llm, op, remote = make_agent(replies, ["Microsoft 365", YES, YES, "user@corp.ru", YES], outputs)

    assert agent.run() is True
    assert remote.terminals == [False]
    assert remote.commands[1:] == ["Get-Package *Office*", "winget install Microsoft.Office -e"]
    assert [a[0] for a in op.asks] == ["question", "plan", "confirm", "question", "done"]
    assert "done" in op.kinds() and "warning" in op.kinds()
    # в промт попали данные пробы, ответы техника и точный вывод команды
    last = llm.requests[-1][1]["content"]
    assert "BUH-01" in last and "Microsoft 365" in last and "user@corp.ru" in last
    assert "Successfully installed" in llm.requests[4][1]["content"]
    # в шагах без запроса скриншотов картинки не отправляются
    assert all(isinstance(r[1]["content"], str) for r in llm.requests)


def test_screenshot_then_click_and_rejected_command():
    replies = [
        {"summary": "", "questions": [], "plan": ["почистить диск"]},
        {"action": "screenshot", "thought": "посмотрю"},
        {"action": "click", "x": 644, "y": 364, "thought": "кликаю по окну"},
        {"action": "run", "command": "Remove-Item C:\\Users\\* -Recurse"},
        {"action": "fail", "reason": "нельзя"},
    ]
    agent, llm, op, remote = make_agent(replies, [YES, "не удаляй профили", STOP])
    assert agent.run() is False
    assert remote.gui == [("click", (644, 364))]
    assert "Remove-Item" not in " ".join(remote.commands)  # техник отклонил
    assert isinstance(llm.requests[2][1]["content"], list)  # шаг с картинкой
    assert "не удаляй профили" in llm.requests[4][1]["content"]


def test_without_vision_gui_is_refused():
    replies = [
        {"summary": "", "questions": [], "plan": ["x"]},
        {"action": "screenshot"},
        {"action": "done", "summary": "ok"},
    ]
    agent, llm, op, remote = make_agent(replies, [YES], vision=False, confirm="none")
    assert agent.run() is True
    assert "not available" in llm.requests[2][1]["content"]


def test_console_not_answering_triggers_screenshot():
    replies = [
        {"summary": "", "questions": [], "plan": ["x"]},
        {"action": "fail", "reason": "консоль не отвечает"},
    ]
    # NO на вопрос «откройте PowerShell вручную» — продолжить без консоли, по скриншоту
    agent, llm, op, remote = make_agent(replies, [NO, YES, NO], silent=True)
    assert agent.run() is False
    assert "NO RESPONSE" in llm.requests[1][1]["content"][0]["text"]  # и скриншот приложен
