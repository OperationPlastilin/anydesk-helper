"""Запуск AnyDesk, поиск окна сеанса и область экрана, с которой работает агент."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

from .screen import Region, primary_monitor, virtual_screen

DOWNLOAD_URL = "https://download.anydesk.com/AnyDesk.exe"

CANDIDATE_PATHS = [
    r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe",
    r"C:\Program Files\AnyDesk\AnyDesk.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\AnyDesk\AnyDesk.exe"),
    "/usr/bin/anydesk",
    "/Applications/AnyDesk.app/Contents/MacOS/AnyDesk",
]
# Окна, которые нельзя принять за окно сеанса: наша консоль «AnyDesk Helper», вкладка
# браузера с веб-интерфейсом «AnyDesk Helper - Google Chrome» и т.п.
CONSOLE_WORDS = ("python", "anydesk_helper", "anydesk helper", "cmd.exe", "powershell", "terminal",
                 "ai helper")
# Win32: см. WindowTarget.focus.
SW_RESTORE, VK_MENU, KEYEVENTF_KEYUP = 9, 0x12, 0x0002


class FocusError(RuntimeError):
    """Окно сеанса AnyDesk не удалось вывести на передний план — вводить ничего нельзя."""


def normalize_address(address: str) -> str:
    """'123 456 789' -> '123456789'; алиасы вида 'name@ad' оставляем как есть."""
    address = address.strip()
    return re.sub(r"[\s-]", "", address) if re.fullmatch(r"[\d\s-]+", address) else address


def find_executable(configured: str = "", extra_dirs=()) -> str:
    if configured:
        if not os.path.exists(configured):
            raise FileNotFoundError(f"AnyDesk не найден: {configured}")
        return configured
    for path in [*CANDIDATE_PATHS, *(os.path.join(d, "AnyDesk.exe") for d in extra_dirs)]:
        if os.path.exists(path):
            return path
    found = shutil.which("anydesk") or shutil.which("AnyDesk")
    if found:
        return found
    raise FileNotFoundError("AnyDesk не найден. Укажите путь в настройках или скачайте его.")


def download(dest_dir) -> str:
    """Скачивает портативный AnyDesk с официального сайта (если он не установлен)."""
    import requests

    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "AnyDesk.exe")
    tmp = path + ".part"
    with requests.get(DOWNLOAD_URL, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
    os.replace(tmp, path)
    return path


def connect(exe: str, address: str, password: str | None = None) -> None:
    """Открывает исходящее подключение. Пароль — для неконтролируемого доступа."""
    args = [exe, address]
    if password:
        # Официальный способ: echo <пароль> | AnyDesk.exe <ID> --with-password
        args.append("--with-password")
        proc = subprocess.Popen(args, stdin=subprocess.PIPE)
        proc.stdin.write((password + "\n").encode())
        proc.stdin.close()
    else:
        subprocess.Popen(args)


class ScreenTarget:
    """Весь основной монитор (режим --local, для проверки без AnyDesk)."""

    title = "локальный экран"

    def region(self) -> Region:
        return primary_monitor()

    def focus(self) -> None:
        pass


class WindowTarget:
    """Окно сеанса AnyDesk. Область пересчитывается каждый шаг: окно могут двигать."""

    def __init__(self, window, sleep=time.sleep):
        self.window = window
        self.title = window.title
        self.sleep = sleep

    def region(self) -> Region:
        w = self.window
        screen = virtual_screen()
        left = max(w.left, screen.left)
        top = max(w.top, screen.top)
        right = min(w.left + w.width, screen.left + screen.width)
        bottom = min(w.top + w.height, screen.top + screen.height)
        if right - left < 50 or bottom - top < 50:
            raise RuntimeError("Окно AnyDesk свёрнуто или за пределами экрана — разверните его")
        return Region(left, top, right - left, bottom - top)

    def focus(self) -> None:
        """Выводит окно сеанса на передний план и ПРОВЕРЯЕТ это (GetForegroundWindow).
        Windows отдаёт передний план только процессу, от которого был последний ввод, а его
        обычно забирает клик техника в браузере. Не вышло — FocusError, а не ввод вслепую."""
        u = _user32()
        if not u.IsWindow(self.window._hWnd) and not self._rebind(u):
            raise FocusError(f"Окно сеанса AnyDesk «{self.title}» закрыто. Переподключитесь к "
                             "пользователю (окно с тем же заголовком) и повторите — или "
                             "остановите и запустите сеанс заново")
        hwnd = self.window._hWnd
        for attempt in range(3):
            if not u.IsWindow(hwnd):
                raise FocusError("Окно сеанса AnyDesk закрылось — переподключитесь и повторите")
            if u.GetForegroundWindow() == hwnd:
                return
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, SW_RESTORE)
            if attempt:
                # Нажатие Alt делает нас «последним вводом» и снимает запрет. Нажимаем и
                # отпускаем ДО переключения, чтобы отпускание Alt не ушло через AnyDesk.
                u.keybd_event(VK_MENU, 0, 0, 0)
                u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            u.SetForegroundWindow(hwnd)
            self.sleep(0.2)
        if u.GetForegroundWindow() != hwnd:
            raise FocusError("Не удалось вывести окно AnyDesk на передний план — кликните по нему")

    def _rebind(self, u) -> bool:
        """Окно сеанса закрылось (пользователь отключился, перезагрузка) — после переподключения
        AnyDesk открывает новое окно с тем же заголовком. Берём его, только если оно одно и
        заголовок не общий («AnyDesk» — это и главное окно AnyDesk на компьютере техника)."""
        title = self.title.strip().lower()
        if title in ("", "anydesk", "anydesk.exe") or any(word in title for word in CONSOLE_WORDS):
            return False
        found = [w for w in _windows() if w.title.strip().lower() == title and u.IsWindow(w._hWnd)]
        if len(found) != 1:
            return False
        self.window = found[0]
        return True


def _user32():
    import ctypes

    return ctypes.windll.user32


def _windows():
    if sys.platform != "win32":
        return []
    import pygetwindow

    return [w for w in pygetwindow.getAllWindows() if w.title and w.width > 200]


def find_session_window(address: str, title_hint: str = ""):
    digits = normalize_address(address).lower()
    title_hint = title_hint.strip()
    if title_hint.lower() in ("anydesk", "anydesk.exe"):
        title_hint = ""  # подходит к любому окну AnyDesk (и к главному) — ищем по ID
    for w in _windows():
        title = w.title.lower()
        if any(word in title for word in CONSOLE_WORDS):
            continue
        squashed = re.sub(r"[\s-]", "", title)
        if title_hint and title_hint.lower() in title:
            return w
        if not title_hint and digits and digits in squashed:
            return w
    return None


def pick_active_window(delay: int = 5):
    """Техник за delay секунд кликает по окну сеанса — берём активное окно."""
    if sys.platform != "win32":
        return None
    import pygetwindow

    time.sleep(delay)
    window = pygetwindow.getActiveWindow()
    if window and any(word in window.title.lower() for word in ("chrome", "firefox", "edge", "opera", "yandex")):
        return None  # кликнули по браузеру с веб-интерфейсом, а не по AnyDesk
    return window


def set_console_title(title: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)


def enable_dpi_awareness() -> None:
    """Без этого на мониторах с масштабом 125–150% клики промахиваются."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
