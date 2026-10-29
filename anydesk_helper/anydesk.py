"""Запуск AnyDesk, поиск окна сеанса и область экрана, с которой работает агент."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

from .screen import Region, primary_monitor, virtual_screen

CANDIDATE_PATHS = [
    r"C:\Program Files (x86)\AnyDesk\AnyDesk.exe",
    r"C:\Program Files\AnyDesk\AnyDesk.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\AnyDesk\AnyDesk.exe"),
    "/usr/bin/anydesk",
    "/Applications/AnyDesk.app/Contents/MacOS/AnyDesk",
]
# Окна, которые нельзя принять за окно сеанса (наша же консоль и т.п.).
CONSOLE_WORDS = ("python", "anydesk_helper", "cmd.exe", "powershell", "terminal", "ai helper")


def normalize_address(address: str) -> str:
    """'123 456 789' -> '123456789'; алиасы вида 'name@ad' оставляем как есть."""
    address = address.strip()
    return re.sub(r"[\s-]", "", address) if re.fullmatch(r"[\d\s-]+", address) else address


def find_executable(configured: str = "") -> str:
    if configured:
        if not os.path.exists(configured):
            raise FileNotFoundError(f"AnyDesk не найден: {configured}")
        return configured
    for path in CANDIDATE_PATHS:
        if os.path.exists(path):
            return path
    found = shutil.which("anydesk") or shutil.which("AnyDesk")
    if found:
        return found
    raise FileNotFoundError("AnyDesk не найден. Укажите путь в config.toml: [anydesk] path = \"...\"")


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

    def __init__(self, window):
        self.window = window
        self.title = window.title

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
        try:
            if self.window.isMinimized:
                self.window.restore()
            self.window.activate()
        except Exception:
            # pygetwindow на Windows иногда бросает исключение, хотя окно активировалось.
            pass
        time.sleep(0.2)


def _windows():
    if sys.platform != "win32":
        return []
    import pygetwindow

    return [w for w in pygetwindow.getAllWindows() if w.title and w.width > 200]


def find_session_window(address: str, title_hint: str = ""):
    digits = normalize_address(address).lower()
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


def wait_for_session(address: str, timeout: int, title_hint: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        window = find_session_window(address, title_hint)
        if window:
            return window
        time.sleep(2)
    return None


def pick_active_window(delay: int = 5):
    if sys.platform != "win32":
        return None
    import pygetwindow

    for left in range(delay, 0, -1):
        print(f"  ...{left}", flush=True)
        time.sleep(1)
    return pygetwindow.getActiveWindow()


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
