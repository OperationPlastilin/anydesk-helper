"""Мышь и клавиатура в окне AnyDesk."""

from __future__ import annotations

import string
import sys
import time

from .actions import Action
from .anydesk import FocusError
from .screen import Frame

# pyautogui на Windows передаёт значение как «сырую» дельту колеса: 120 = один щелчок.
WHEEL_NOTCH = 120 if sys.platform == "win32" else 1


def layout_independent_keys(mapping: dict) -> None:
    """pyautogui при импорте заполняет таблицу клавиш через VkKeyScanA по ТЕКУЩЕЙ раскладке.
    В русской нет латинских букв (-1), и Ctrl+V превращается в Ctrl+Alt+Shift+0xFF.
    Коды VK_A..VK_Z от раскладки не зависят: это коды заглавных букв (Shift pyautogui добавит сам).
    Остальные символы, которых нет в раскладке, лучше не нажимать вовсе (None — pyautogui пропустит)."""
    for key, vk in mapping.items():
        if len(key) == 1 and isinstance(vk, int) and vk & 0xFF == 0xFF:
            mapping[key] = None
    for c in string.ascii_lowercase:
        mapping[c] = mapping[c.upper()] = ord(c.upper())


class Controller:
    """target — окно сеанса: перед каждым нажатием и кликом выводим его на передний план
    (target.focus() бросает anydesk.FocusError, если не вышло), чтобы ввод не ушёл в
    браузер или консоль на компьютере техника."""

    def __init__(self, dry_run: bool = False, target=None, clipboard=None, sleep=time.sleep):
        self.dry_run = dry_run
        self.target = target
        self.clipboard = clipboard  # объект с copy(); по умолчанию pyperclip
        self.sleep = sleep
        self.line_dirty = False  # строку вставили в консоль, а Enter не нажали (потеряли фокус)
        self._gui = None

    @property
    def gui(self):
        # Импорт откладываем: pyautogui требует графический экран уже при импорте.
        if self._gui is None:
            import pyautogui

            pyautogui.FAILSAFE = True  # мышь в угол экрана — аварийная остановка
            pyautogui.PAUSE = 0.08
            if sys.platform == "win32":
                layout_independent_keys(pyautogui.platformModule.keyboardMapping)
            self._gui = pyautogui
        return self._gui

    def _focus(self) -> None:
        if self.target is not None:
            self.target.focus()

    def execute(self, a: Action, frame: Frame | None) -> str:
        """Выполняет GUI-действие. Координаты действия — в пикселях кадра frame."""
        if self.dry_run:
            return "dry-run: not executed"
        g = self.gui
        if a.type in ("click", "double_click", "right_click", "drag", "scroll"):
            self._focus()
            if self.target is not None and frame is not None and self.target.region() != frame.region:
                # Пока ждали модель или ответ техника, окно сдвинули: старые координаты мимо.
                raise RuntimeError("the AnyDesk window moved since the screenshot; "
                                   "nothing was clicked, look at the new screenshot")
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
                self.sleep(0.03)
        elif a.type == "type":
            self.paste(a.text)
            if a.enter:
                self.sleep(0.2)
                try:
                    self.hotkey("enter")
                except FocusError as e:
                    # Текст уже вставлен: FocusError -> агент повторил бы действие и вставил его дважды.
                    raise RuntimeError("the text was typed but Enter was NOT pressed (the AnyDesk "
                                       "window lost focus); look at the new screenshot and press "
                                       "enter with a key action if needed") from e
        elif a.type == "key":
            self.hotkey(*a.keys)
        else:
            raise ValueError(f"Controller can't execute {a.type}")
        return "ok"

    def paste(self, text: str) -> None:
        """Ввод через буфер обмена: AnyDesk передаёт его на ту сторону, поэтому не важны
        раскладка пользователя и кириллица (сам Ctrl+V — через layout_independent_keys)."""
        if self.dry_run:
            return
        if self.clipboard is None:
            import pyperclip

            self.clipboard = pyperclip
        self.clipboard.copy(text)
        self.sleep(0.5)  # даём AnyDesk передать буфер
        self._focus()  # за эти полсекунды техник мог переключиться на другое окно
        self.gui.hotkey("ctrl", "v")

    def paste_line(self, text: str) -> None:
        """Строка в консоль + Enter. Если прошлую строку вставили, а перед Enter окно потеряло
        фокус (FocusError), она так и осталась в строке ввода: сначала Esc (очистка строки и в
        conhost, и в PSReadLine), иначе повтор допишет вторую обёртку к первой."""
        if self.dry_run:
            return
        if self.line_dirty:
            self.hotkey("esc")
            self.line_dirty = False
        self.paste(text)
        self.line_dirty = True
        self.sleep(0.3)
        self.hotkey("enter")
        self.line_dirty = False

    def hotkey(self, *keys: str) -> None:
        if self.dry_run:
            return
        self._focus()
        if len(keys) == 1:
            self.gui.press(keys[0])
        else:
            self.gui.hotkey(*keys, interval=0.05)

    def open_terminal(self, admin: bool) -> None:
        """Win+R -> powershell. Для администратора Ctrl+Shift+Enter -> окно UAC."""
        if self.dry_run:
            return
        self.hotkey("win", "r")
        self.sleep(1.2)
        self.paste("powershell")
        self.sleep(0.3)
        if admin:
            self.hotkey("ctrl", "shift", "enter")
        else:
            self.hotkey("enter")
        self.sleep(2.5)
