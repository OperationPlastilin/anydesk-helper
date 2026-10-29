"""Выполнение действий мышью и клавиатурой в окне AnyDesk."""

from __future__ import annotations

import sys
import time

from .actions import Action
from .screen import Frame

# pyautogui на Windows передаёт значение как «сырую» дельту колеса: 120 = один щелчок.
WHEEL_NOTCH = 120 if sys.platform == "win32" else 1


class Controller:
    def __init__(self, typing: str = "clipboard", dry_run: bool = False):
        self.typing = typing
        self.dry_run = dry_run
        self._gui = None

    @property
    def gui(self):
        # Импорт откладываем: pyautogui требует графический экран уже при импорте.
        if self._gui is None:
            import pyautogui

            pyautogui.FAILSAFE = True  # мышь в угол экрана — аварийная остановка
            pyautogui.PAUSE = 0.08
            self._gui = pyautogui
        return self._gui

    def execute(self, a: Action, frame: Frame) -> str:
        if self.dry_run:
            return "dry-run: not executed"
        g = self.gui
        if a.type in ("click", "double_click", "right_click"):
            x, y = frame.to_screen(a.x, a.y)
            g.moveTo(x, y, duration=0.15)
            if a.type == "double_click":
                g.doubleClick(x, y)
            else:
                g.click(x, y, button="right" if a.type == "right_click" else "left")
        elif a.type == "drag":
            x, y = frame.to_screen(a.x, a.y)
            x2, y2 = frame.to_screen(a.x2, a.y2)
            g.moveTo(x, y, duration=0.15)
            g.dragTo(x2, y2, duration=0.6, button="left")
        elif a.type == "scroll":
            x, y = frame.to_screen(a.x, a.y)
            g.moveTo(x, y, duration=0.1)
            for _ in range(abs(a.amount)):  # по одному щелчку колеса — AnyDesk так надёжнее
                g.scroll(WHEEL_NOTCH if a.amount > 0 else -WHEEL_NOTCH)
                time.sleep(0.03)
        elif a.type == "type":
            self.type_text(a.text)
            if a.enter:
                time.sleep(0.2)
                g.press("enter")
        elif a.type == "key":
            if len(a.keys) == 1:
                g.press(a.keys[0])
            else:
                g.hotkey(*a.keys, interval=0.05)
        elif a.type == "open_terminal":
            self.open_terminal(a.shell, a.admin)
        elif a.type == "wait":
            time.sleep(a.seconds)
        else:
            raise ValueError(f"Controller can't execute {a.type}")
        return "ok"

    def type_text(self, text: str) -> None:
        if self.typing == "clipboard":
            # AnyDesk синхронизирует буфер обмена, а вставка не зависит от раскладки
            # клавиатуры на удалённом ПК и работает с кириллицей.
            import pyperclip

            pyperclip.copy(text)
            time.sleep(0.4)  # даём AnyDesk передать буфер на ту сторону
            self.gui.hotkey("ctrl", "v")
        else:
            self.gui.write(text, interval=0.02)

    def open_terminal(self, shell: str, admin: bool) -> None:
        g = self.gui
        g.hotkey("win", "r")
        time.sleep(1.2)
        self.type_text(shell)
        time.sleep(0.3)
        if admin:
            g.hotkey("ctrl", "shift", "enter")  # запуск от администратора -> окно UAC
        else:
            g.press("enter")
        time.sleep(2.5)
