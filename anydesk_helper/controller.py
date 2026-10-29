"""Мышь и клавиатура в окне AnyDesk."""

from __future__ import annotations

import sys
import time

from .actions import Action
from .screen import Frame

# pyautogui на Windows передаёт значение как «сырую» дельту колеса: 120 = один щелчок.
WHEEL_NOTCH = 120 if sys.platform == "win32" else 1


class Controller:
    def __init__(self, dry_run: bool = False):
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

    def execute(self, a: Action, frame: Frame | None) -> str:
        """Выполняет GUI-действие. Координаты действия — в пикселях кадра frame."""
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
            self.paste(a.text)
            if a.enter:
                time.sleep(0.2)
                g.press("enter")
        elif a.type == "key":
            self.hotkey(*a.keys)
        else:
            raise ValueError(f"Controller can't execute {a.type}")
        return "ok"

    def paste(self, text: str) -> None:
        """Ввод через буфер обмена: AnyDesk передаёт его на ту сторону, поэтому
        не важны раскладка клавиатуры пользователя и кириллица."""
        if self.dry_run:
            return
        import pyperclip

        pyperclip.copy(text)
        time.sleep(0.5)  # даём AnyDesk передать буфер
        self.gui.hotkey("ctrl", "v")

    def paste_line(self, text: str) -> None:
        if self.dry_run:
            return
        self.paste(text)
        time.sleep(0.3)
        self.gui.press("enter")

    def hotkey(self, *keys: str) -> None:
        if self.dry_run:
            return
        if len(keys) == 1:
            self.gui.press(keys[0])
        else:
            self.gui.hotkey(*keys, interval=0.05)

    def open_terminal(self, admin: bool) -> None:
        """Win+R -> powershell. Для администратора Ctrl+Shift+Enter -> окно UAC."""
        if self.dry_run:
            return
        self.hotkey("win", "r")
        time.sleep(1.2)
        self.paste("powershell")
        time.sleep(0.3)
        if admin:
            self.hotkey("ctrl", "shift", "enter")
        else:
            self.hotkey("enter")
        time.sleep(2.5)
