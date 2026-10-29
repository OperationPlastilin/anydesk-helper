"""Автообновление из GitHub Releases.

При запуске программа смотрит последний релиз репозитория. Если версия новее, скачивает
AnyDeskHelper.exe, сверяет SHA-256 с файлом AnyDeskHelper.exe.sha256 из того же релиза
и подменяет себя через маленький .bat (работающий .exe нельзя перезаписать изнутри).
"""

from __future__ import annotations

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
    data = resp.json()
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(current):
        return None
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if ASSET not in assets:
        return None  # релиз ещё собирается
    return {"version": tag.lstrip("v"), "url": assets[ASSET],
            "sha256_url": assets.get(ASSET + ".sha256"),
            "notes": (data.get("body") or "")[:3000], "page": data.get("html_url", "")}


def can_self_update() -> bool:
    return is_frozen() and sys.platform == "win32"


def download(info: dict, dest: Path, http=requests) -> None:
    expected = None
    if info.get("sha256_url"):
        resp = http.get(info["sha256_url"], timeout=30)
        resp.raise_for_status()
        expected = resp.text.split()[0].lower()
    digest = hashlib.sha256()
    tmp = dest.with_suffix(".part")
    with http.get(info["url"], stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
                digest.update(chunk)
    if expected and digest.hexdigest() != expected:
        tmp.unlink(missing_ok=True)
        raise UpdateError("Контрольная сумма обновления не совпала — файл повреждён, обновление отменено")
    os.replace(tmp, dest)


SWAP_BAT = """@echo off
chcp 65001 >nul
set n=0
:retry
timeout /t 1 /nobreak >nul
move /y "{new}" "{exe}" >nul 2>&1 && goto ok
set /a n+=1
if %n% lss 60 goto retry
exit /b 1
:ok
start "" "{exe}" {args}
(goto) 2>nul & del "%~f0"
"""


def install(info: dict, restart_args: list[str] | None = None, exit_after: float | None = 1.0) -> None:
    """Скачивает новую версию, запускает подмену .exe и завершает текущий процесс."""
    if not can_self_update():
        raise UpdateError("Автообновление работает в собранной программе AnyDeskHelper.exe. "
                          "Если вы запускаете из исходников — выполните git pull.")
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    try:
        download(info, new)
    except requests.RequestException as e:
        raise UpdateError(f"Не удалось скачать обновление: {e}") from None
    args = subprocess.list2cmdline(restart_args or [])
    bat = Path(tempfile.gettempdir()) / f"anydesk-helper-update-{os.getpid()}.bat"
    # UTF-8 + chcp 65001: путь может содержать кириллицу (C:\Users\Иван\...).
    bat.write_text(SWAP_BAT.format(new=new, exe=exe, args=args), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
    if exit_after is not None:
        threading.Timer(exit_after, os._exit, [0]).start()  # даём веб-серверу ответить
