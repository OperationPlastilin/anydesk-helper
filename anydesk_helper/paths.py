"""Где лежат ресурсы программы и данные пользователя (тикеты, настройки, логи)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AnyDeskHelper"


def is_frozen() -> bool:
    """Запущены из собранного .exe (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Папка с файлами, которые идут вместе с программой (web, playbooks)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("ANYDESK_HELPER_DATA")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        path = Path.home() / ".anydesk-helper"
    path.mkdir(parents=True, exist_ok=True)
    return path
