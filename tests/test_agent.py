import json

import pytest
from PIL import Image

from anydesk_helper import screen
from anydesk_helper.actions import (ActionError, extract_json, needs_confirmation,
                                    normalize_keys, parse_action)
from anydesk_helper.agent import Agent
from anydesk_helper.config import Config
from anydesk_helper.controller import Controller
from anydesk_helper.prompts import load_playbooks
from anydesk_helper.screen import Frame, Region


# ---------- разбор ответов модели ----------

def test_extract_json_from_noisy_reply():
    reply = '<think>hmm {not json}</think>Sure!\n```json\n{"action": "wait", "seconds": 3}\n```'
    assert extract_json(reply) == {"action": "wait", "seconds": 3}


def test_extract_json_fails_without_object():
    with pytest.raises(ActionError):
        extract_json("I will click the button")


def test_parse_click_pixels_and_qwen_bbox():
    a = parse_action({"action": "click", "x": 100, "y": 50}, 800, 600)
    assert (a.type, a.x, a.y) == ("click", 100, 50)
    b = parse_action({"action": "click", "bbox_2d": [10, 20, 30, 40]}, 800, 600)
    assert (b.x, b.y) == (20, 30)
    r = parse_action({"action": "click", "x": 1, "y": 1, "button": "right"}, 800, 600)
    assert r.type == "right_click"


def test_parse_norm1000_converts_to_pixels():
    a = parse_action({"action": "double_click", "x": 500, "y": 250}, 800, 600, "norm1000")
    assert (a.x, a.y) == (400, 150)


def test_parse_rejects_bad_actions():
    with pytest.raises(ActionError):
        parse_action({"action": "explode"}, 800, 600)
    with pytest.raises(ActionError):
        parse_action({"action": "click", "x": 900, "y": 10}, 800, 600)
    with pytest.raises(ActionError):
        parse_action({"action": "key", "keys": ["ctrl", "banana"]}, 800, 600)


def test_normalize_keys():
    assert normalize_keys("Ctrl+Shift+Esc") == ["ctrl", "shift", "esc"]
    assert normalize_keys(["Windows", "R"]) == ["win", "r"]
    assert normalize_keys("Return") == ["enter"]


def test_confirmation_rules():
    cmd = parse_action({"action": "type", "text": "Remove-Item C:\\x -Recurse", "enter": True}, 9, 9)
    safe = parse_action({"action": "type", "text": "notepad", "enter": True}, 9, 9)
    enter = parse_action({"action": "key", "keys": ["enter"]}, 9, 9)
    click = parse_action({"action": "click", "x": 1, "y": 1}, 9, 9)
    flagged = parse_action({"action": "click", "x": 1, "y": 1, "risky": True}, 9, 9)
    assert needs_confirmation(cmd, "risky")
    assert not needs_confirmation(safe, "risky")
    assert needs_confirmation(enter, "risky", last_typed="shutdown /r /t 0")
    assert not needs_confirmation(click, "risky")
    assert needs_confirmation(flagged, "risky")
    assert needs_confirmation(click, "all")
    assert not needs_confirmation(cmd, "none")


# ---------- координаты ----------

def test_fit_size_multiple_of_28():
    w, h = screen.fit_size(1920, 1080, 1288)
    assert w % 28 == 0 and h % 28 == 0 and max(w, h) <= 1288


def test_frame_maps_to_screen_with_offset():
    frame = Frame(Image.new("RGB", (640, 360)), Region(100, 50, 1280, 720))
    assert frame.to_screen(320, 180) == (740, 410)
    assert frame.to_screen(640, 360) == (1379, 769)  # не выходит за окно


# ---------- методички ----------

def test_playbooks_selected_by_keywords(tmp_path):
    (tmp_path / "all.md").write_text("keywords: *\nобщие советы", encoding="utf-8")
    (tmp_path / "office.md").write_text("keywords: office, офис, word\nпро офис", encoding="utf-8")
    assert "про офис" in load_playbooks(tmp_path, "Установить Офис пользователю")
    text = load_playbooks(tmp_path, "сбросить password")
    assert "общие советы" in text and "про офис" not in text


# ---------- полный цикл агента с поддельной моделью ----------

class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages):
        self.requests.append(messages)
        return self.replies.pop(0)


class FakeConsole:
    def __init__(self, answers):
        self.answers = list(answers)
        self.output = []

    def say(self, text=""):
        self.output.append(text)

    def ask(self, prompt):
        self.output.append(prompt)
        return self.answers.pop(0)


class FakeTarget:
    def region(self):
        return Region(0, 0, 1288, 728)

    def focus(self):
        pass


class RecordingController(Controller):
    def __init__(self):
        super().__init__(dry_run=True)
        self.done = []

    def execute(self, a, frame):
        self.done.append((a.type, frame.to_screen(a.x, a.y) if a.x is not None else a.text))
        return "ok"


def test_agent_full_session(tmp_path):
    cfg = Config()
    cfg.agent.step_delay = 0
    cfg.agent.playbooks_dir = str(tmp_path)
    replies = [
        json.dumps({"understanding": "поставить Office", "questions": ["Какая лицензия?"],
                    "plan": ["открыть PowerShell", "запустить установку"]}),
        "not json at all",  # модель ошиблась — агент переспросит
        json.dumps({"thought": "открываю консоль", "action": "open_terminal", "shell": "powershell"}),
        json.dumps({"thought": "", "action": "click", "x": 644, "y": 364}),
        json.dumps({"thought": "ставлю", "action": "type", "text": "winget install Microsoft.Office",
                    "enter": True, "risky": True}),
        json.dumps({"action": "ask_user", "question": "Логин учётки?"}),
        json.dumps({"action": "done", "summary": "Office установлен"}),
    ]
    llm = FakeLLM(replies)
    # ответы оператора: лицензия, план ок, подтверждение risky-команды, логин, «задача решена»
    console = FakeConsole(["Microsoft 365", "", "y", "user@corp.ru", "y"])
    controller = RecordingController()
    agent = Agent(cfg, llm, FakeTarget(), controller, console, grab=lambda r: Image.new(
        "RGB", (r.width, r.height), "navy"))

    assert agent.run("установить Microsoft Office") is True
    assert controller.done == [
        ("open_terminal", ""),
        ("click", (644, 364)),
        ("type", "winget install Microsoft.Office"),
    ]
    last_prompt = llm.requests[-1][1]["content"][0]["text"]
    assert "Microsoft 365" in last_prompt and "user@corp.ru" in last_prompt
    assert llm.requests[-1][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_agent_operator_rejects_risky_action(tmp_path):
    cfg = Config()
    cfg.agent.step_delay = 0
    cfg.agent.playbooks_dir = str(tmp_path)
    replies = [
        json.dumps({"plan": ["почистить диск"], "questions": []}),
        json.dumps({"action": "type", "text": "Remove-Item C:\\Users -Recurse", "enter": True}),
        json.dumps({"action": "fail", "reason": "нельзя"}),
    ]
    console = FakeConsole(["", "не удаляй профили", ""])
    controller = RecordingController()
    agent = Agent(cfg, FakeLLM(replies), FakeTarget(), controller, console,
                  grab=lambda r: Image.new("RGB", (r.width, r.height)))
    assert agent.run("очистить диск") is False
    assert controller.done == []
