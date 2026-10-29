"""Подделки для тестов: модель, консоль пользователя через «буфер обмена», оператор."""

import base64
import json
import re

from PIL import Image

from anydesk_helper.bridge import END, START
from anydesk_helper.screen import Region


class FakeLLM:
    def __init__(self, replies):
        self.replies = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in replies]
        self.requests = []

    def chat(self, messages):
        self.requests.append(messages)
        if not self.replies:
            raise AssertionError("FakeLLM: ответы закончились")
        return self.replies.pop(0)


class FakeClipboard:
    def __init__(self):
        self.text = ""

    def copy(self, text):
        self.text = text

    def paste(self):
        return self.text


class FakeRemote:
    """Имитирует PowerShell пользователя: получает вставленную обёртку, «выполняет» команду
    по таблице ответов и кладёт результат в общий буфер обмена, как настоящая обёртка."""

    def __init__(self, clipboard, outputs=None, silent=False, running=False):
        self.clipboard = clipboard
        self.outputs = outputs or {}
        self.silent = silent  # консоль не в фокусе — ничего не происходит
        self.running = running  # команда стартовала, но не закончилась (bool или regex)
        self.commands = []
        self.terminals = []
        self.keys = []
        self.gui = []

    # интерфейс Controller
    def paste_line(self, line):
        if self.silent:
            return
        b64 = re.search(r"FromBase64String\('([^']+)'\)", line).group(1)
        cid = re.search(rf"'{START}(\w+)'", line).group(1)
        command = base64.b64decode(b64).decode("utf-8")
        self.commands.append(command)
        # running=True — любая команда «зависает»; строка — только команды, подходящие под шаблон
        if self.running is True or (isinstance(self.running, str) and re.search(self.running, command)):
            self.clipboard.text = START + cid
            return
        output, code = "", 0
        for pattern, result in self.outputs.items():
            if re.search(pattern, command):
                output, code = result if isinstance(result, tuple) else (result, 0)
                break
        self.clipboard.text = f"{END}{cid} exit={code}\n{output}"

    def open_terminal(self, admin):
        self.terminals.append(admin)

    def hotkey(self, *keys):
        self.keys.append(keys)

    def execute(self, action, frame):
        self.gui.append((action.type, frame.to_screen(action.x, action.y) if action.x is not None else None))
        return "ok"


class FakeTarget:
    def region(self):
        return Region(0, 0, 1288, 728)

    def focus(self):
        pass


def fake_grab(region):
    return Image.new("RGB", (region.width, region.height), "navy")


class FakeOperator:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.events = []
        self.asks = []
        self.hints = []

    def event(self, kind, text="", **data):
        self.events.append((kind, text, data))

    def ask(self, kind, text, **data):
        self.asks.append((kind, text, data))
        if not self.answers:
            raise AssertionError(f"FakeOperator: нет ответа на {kind}: {text}")
        return self.answers.pop(0)

    def checkpoint(self):
        hints, self.hints = self.hints, []
        return hints

    def image(self, step, img):
        return f"step_{step}.jpg"

    def kinds(self):
        return [e[0] for e in self.events]


class Clock:
    """Время, которое идёт только когда бридж «спит» — тесты без реальных пауз."""

    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t
