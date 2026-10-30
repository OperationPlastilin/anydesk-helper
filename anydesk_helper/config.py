"""Настройки программы. Хранятся в settings.json в папке данных, меняются из веб-интерфейса."""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

# Бесплатные облачные провайдеры с OpenAI-совместимым API.
PRESETS = {
    "openrouter": {
        "title": "OpenRouter (бесплатные модели :free)",
        "base_url": "https://openrouter.ai/api/v1",
        # auto — программа сама выберет лучшую бесплатную Qwen-VL: бесплатные модели OpenRouter часто
        # меняются (так исчезла qwen/qwen2.5-vl-72b-instruct:free), а жёстко прописанная ломает запуск.
        "model": "auto",
        "coords": "pixels",
        "key_url": "https://openrouter.ai/keys",
    },
    "dashscope": {
        "title": "Alibaba Model Studio / DashScope (бесплатная квота)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max",
        "coords": "pixels",
        "key_url": "https://modelstudio.console.alibabacloud.com/",
    },
    "custom": {
        "title": "Другой OpenAI-совместимый сервер",
        "base_url": "",
        "model": "",
        "coords": "pixels",
        "key_url": "",
    },
}

CHOICES = {
    "provider": tuple(PRESETS),
    "coords": ("pixels", "norm1000"),
    "confirm": ("all", "risky", "none"),
}

# Допустимые числа: порт вне диапазона уронил бы запуск ещё до веб-интерфейса.
RANGES = {
    "port": (1024, 65535),
    "temperature": (0.0, 2.0),
    "max_tokens": (64, 131072),
    "timeout": (5, 3600),
    "max_steps": (1, 1000),
    "step_delay": (0.0, 60.0),
    "screenshot_max_side": (280, 8192),
    "command_timeout": (5, 7200),
    "keep_files_days": (0, 36500),
    "experience_limit": (0, 50),
    "connect_timeout": (5, 3600),
}


@dataclass
class Settings:
    # --- модель ---
    provider: str = "openrouter"
    base_url: str = PRESETS["openrouter"]["base_url"]
    api_key: str = ""
    model: str = PRESETS["openrouter"]["model"]
    fallback_model: str = ""  # запасная модель, когда закончился дневной лимит бесплатной
    vision: bool = True  # модель видит картинки -> доступны скриншоты и мышь
    # "pixels" — модель отвечает пикселями скриншота (Qwen2.5-VL, qwen-vl-max),
    # "norm1000" — координатами 0..1000 (Qwen3-VL)
    coords: str = "pixels"
    json_mode: bool = True  # просить у API строго JSON (response_format)
    temperature: float = 0.1
    max_tokens: int = 4096  # потолок ответа; для thinking-моделей клиент сам поднимет до 8192
    timeout: int = 120
    # --- агент ---
    confirm: str = "risky"  # all | risky | none
    language: str = "Russian"
    max_steps: int = 80
    step_delay: float = 1.0
    screenshot_max_side: int = 1288
    command_timeout: int = 120
    keep_files_days: int = 30  # скриншоты старше — удаляются при запуске (0 — хранить вечно)
    learning: bool = True  # после сеанса извлекать уроки в базу опыта
    experience_limit: int = 5  # сколько уроков из базы класть в промт
    # --- AnyDesk ---
    anydesk_path: str = ""
    connect_timeout: int = 90
    window_title: str = ""
    # --- программа ---
    port: int = 8765
    auto_update: bool = True

    def public(self) -> dict:
        """Для веб-интерфейса: ключ API не отдаём целиком."""
        data = asdict(self)
        key = data.pop("api_key")
        data["api_key_set"] = bool(key)
        data["api_key_hint"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ""
        return data


def validate(values: dict) -> dict:
    """Приводит типы и проверяет значения. Возвращает только известные поля."""
    known = {f.name: f for f in fields(Settings)}
    defaults = Settings()
    clean = {}
    for key, value in values.items():
        if key not in known:
            continue
        default = getattr(defaults, key)
        try:
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            else:
                value = str(value).strip()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Неверное значение для {key}: {value!r}") from None
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} должен быть одним из: {', '.join(CHOICES[key])}")
        if key in RANGES and not RANGES[key][0] <= value <= RANGES[key][1]:  # NaN тоже не пройдёт
            raise ValueError(f"{key} должен быть от {RANGES[key][0]} до {RANGES[key][1]}")
        if key == "base_url" and value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("Адрес API должен начинаться с https:// (или http://)")
        clean[key] = value
    return clean


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, use_env: bool = True) -> Settings:
        s = Settings()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            # Каждое поле отдельно: неверное или устаревшее значение (правка руками, удалённый
            # пресет) — оставляем по умолчанию, а не роняем запуск. Ошибки покажет только форма.
            for key, value in data.items():
                if value is None:
                    continue
                try:
                    clean = validate({key: value})
                except ValueError:
                    continue
                for k, v in clean.items():
                    setattr(s, k, v)
        # Ключ зашифрован DPAPI: на другом ПК или под другим пользователем он не расшифруется — тогда пусто.
        s.api_key = unprotect(s.api_key)
        if use_env and not s.api_key:
            s.api_key = os.environ.get("ANYDESK_HELPER_API_KEY", "")
        return s

    def update(self, values: dict) -> Settings:
        s = self.load(use_env=False)  # ключ из переменной окружения в файл не пишем
        values = dict(values)
        if not values.get("api_key"):
            values.pop("api_key", None)  # пустое поле в форме = не менять ключ
        clean = validate(values)
        # Сменили провайдера или сервер API без нового ключа — старый ключ туда не отправляем.
        if not clean.get("api_key") and (
                clean.get("provider", s.provider) != s.provider
                or _host(clean.get("base_url", s.base_url)) != _host(s.base_url)):
            s.api_key = ""
        for key, value in clean.items():
            setattr(s, key, value)
        self.save(s)
        return self.load()

    def save(self, s: Settings) -> None:
        tmp = self.path.with_suffix(".tmp")
        data = asdict(s)
        data["api_key"] = protect(data["api_key"])
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return url


# ---------------------------------------------------------------- шифрование ключа (Windows DPAPI)

DPAPI_PREFIX = "dpapi:"


def dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)):
        raise OSError("DPAPI: не удалось обработать ключ")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def protect(secret: str) -> str:
    """Шифрует API-ключ для settings.json. Вне Windows хранит как есть."""
    if not secret or secret.startswith(DPAPI_PREFIX) or not dpapi_available():
        return secret
    return DPAPI_PREFIX + base64.b64encode(_dpapi(secret.encode("utf-8"), encrypt=True)).decode("ascii")


def unprotect(value: str) -> str:
    if not value.startswith(DPAPI_PREFIX):
        return value  # старый файл с открытым ключом — зашифруется при следующем сохранении
    if not dpapi_available():
        return ""
    try:
        return _dpapi(base64.b64decode(value[len(DPAPI_PREFIX):]), encrypt=False).decode("utf-8")
    except (OSError, ValueError):
        return ""
