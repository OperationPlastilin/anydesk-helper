"""Автообновление из GitHub Releases.

При запуске программа смотрит последний релиз репозитория. Если версия новее, скачивает
AnyDeskHelper.exe, сверяет SHA-256 с файлом AnyDeskHelper.exe.sha256 из того же релиза
и подменяет себя через маленький .bat (работающий .exe нельзя перезаписать изнутри).

Перезапуск onefile-exe: процесс несёт переменные PyInstaller (_PYI_*, у старых версий _MEIPASS2)
с путём к своей временной папке _MEIxxxx. Новый exe лежит по тому же пути и без сброса решил бы,
что он подпроцесс старого, и взял бы эту папку, — а её удаляют при выходе старой версии
(«Failed to load Python DLL»). Поэтому .bat получает окружение без них и с
PYINSTALLER_RESET_ENVIRONMENT=1 (PyInstaller >= 6.9): новый exe распакуется заново.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import requests

from .paths import is_frozen
from .version import REPO

API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "AnyDeskHelper.exe"


class UpdateError(RuntimeError):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def check(current: str, http=requests, timeout: float = 8) -> dict | None:
    """Возвращает сведения о новой версии или None, если обновлять нечего."""
    try:
        resp = http.get(API_URL, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        raise UpdateError(f"GitHub недоступен: {e}") from None
    if resp.status_code == 404:
        return None  # релизов ещё нет
    if resp.status_code != 200:
        raise UpdateError(f"GitHub ответил {resp.status_code}")
    try:  # не JSON (страница прокси), список, нет полей — это не повод падать при запуске
        data = resp.json()
        tag = str(data.get("tag_name") or "")
        assets = {a["name"]: a["browser_download_url"] for a in data.get("assets") or []}
        notes, page = (data.get("body") or "")[:3000], data.get("html_url", "")
    except (ValueError, AttributeError, KeyError, TypeError):
        raise UpdateError("GitHub вернул неожиданный ответ (возможно, мешает прокси)") from None
    if parse_version(tag) <= parse_version(current):
        return None
    if ASSET not in assets:
        return None  # релиз ещё собирается
    return {"version": tag.lstrip("v"), "url": assets[ASSET],
            "sha256_url": assets.get(ASSET + ".sha256"), "notes": notes, "page": page}


def can_self_update() -> bool:
    return is_frozen() and sys.platform == "win32"


def download(info: dict, dest: Path, http=requests) -> None:
    expected = None
    if info.get("sha256_url"):
        resp = http.get(info["sha256_url"], timeout=30)
        resp.raise_for_status()
        words = resp.text.split()
        if not words:
            raise UpdateError("Пустой файл контрольной суммы в релизе — обновление отменено")
        expected = words[0].lower()
    digest = hashlib.sha256()
    tmp = dest.with_suffix(".part")
    try:
        with http.get(info["url"], stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
                    digest.update(chunk)
        if expected and digest.hexdigest() != expected:
            raise UpdateError("Контрольная сумма обновления не совпала — файл повреждён, обновление отменено")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)  # недокачанный файл (сеть, диск полон) не оставляем
        raise


# Только ASCII, без путей: пути и аргументы приходят через окружение (AIH_*), а его cmd
# читает в Unicode — кириллица в пути (C:\Users\Иван\...) не ломается при любой кодовой
# странице. ping вместо timeout: timeout сразу падает, если у консоли нет ввода.
# Не вышло подменить за минуту — запускаем старую версию, чтобы программа не пропала.
SWAP_BAT = """@echo off
set n=0
:retry
ping -n 2 127.0.0.1 >nul
move /y "%AIH_NEW%" "%AIH_EXE%" >nul 2>&1 && goto ok
set /a n+=1
if %n% lss 60 goto retry
del "%AIH_NEW%" >nul 2>&1
:ok
start "" "%AIH_EXE%" %AIH_ARGS%
(goto) 2>nul & del "%~f0"
"""


def restart_env(new: Path, exe: Path, args: list[str]) -> dict:
    """Окружение для .bat и новой версии: без переменных PyInstaller старого процесса."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI_", "_MEIPASS"))}
    env.update(AIH_NEW=str(new), AIH_EXE=str(exe), AIH_ARGS=subprocess.list2cmdline(args),
               PYINSTALLER_RESET_ENVIRONMENT="1")
    return env


def install(info: dict, restart_args: list[str] | None = None, exit_after: float | None = 1.0) -> None:
    """Скачивает новую версию, запускает подмену .exe и завершает текущий процесс."""
    if not can_self_update():
        raise UpdateError("Автообновление работает в собранной программе AnyDeskHelper.exe. "
                          "Если вы запускаете из исходников — выполните git pull.")
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    try:
        download(info, new)
    except OSError as e:  # сеть (requests.RequestException — тоже OSError), нет прав, диск полон
        hint = (" Переместите AnyDeskHelper.exe в папку, где у вас есть права на запись "
                "(например, «Загрузки»)." if isinstance(e, PermissionError) else "")
        raise UpdateError(f"Не удалось скачать обновление: {e}.{hint}") from None
    # --no-update: если exe в релизе окажется старее тега, не будет вечного цикла обновлений.
    args = list(restart_args or [])
    if "--no-update" not in args:
        args.append("--no-update")
    bat = Path(tempfile.gettempdir()) / f"anydesk-helper-update-{os.getpid()}.bat"
    # CREATE_NO_WINDOW, а не DETACHED_PROCESS: у cmd должна быть (скрытая) консоль для ping.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        bat.write_text(SWAP_BAT, encoding="ascii")
        subprocess.Popen(["cmd", "/d", "/c", str(bat)], env=restart_env(new, exe, args),
                         creationflags=flags, close_fds=True)
    except OSError as e:
        with contextlib.suppress(OSError):
            new.unlink(missing_ok=True)
        raise UpdateError(f"Не удалось запустить замену программы: {e}") from None
    if exit_after is not None:
        threading.Timer(exit_after, os._exit, [0]).start()  # даём веб-серверу ответить
