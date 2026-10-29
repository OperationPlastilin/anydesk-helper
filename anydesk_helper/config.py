"""Настройки программы. Хранятся в settings.json в папке данных, меняются из веб-интерфейса."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Бесплатные облачные провайдеры с OpenAI-совместимым API.
PRESETS = {
    "openrouter": {
        "title": "OpenRouter (бесплатные модели :free)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen2.5-vl-72b-instruct:free",
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


@dataclass
class Settings:
    # --- модель ---
    provider: str = "openrouter"
    base_url: str = PRESETS["openrouter"]["base_url"]
    api_key: str = ""
    model: str = PRESETS["openrouter"]["model"]
    vision: bool = True  # модель видит картинки -> доступны скриншоты и мышь
    # "pixels" — модель отвечает пикселями скриншота (Qwen2.5-VL, qwen-vl-max),
    # "norm1000" — координатами 0..1000 (Qwen3-VL)
    coords: str = "pixels"
    json_mode: bool = True  # просить у API строго JSON (response_format)
    temperature: float = 0.1
    max_tokens: int = 1200
    timeout: int = 120
    # --- агент ---
    confirm: str = "risky"  # all | risky | none
    language: str = "Russian"
    max_steps: int = 80
    step_delay: float = 1.0
    screenshot_max_side: int = 1288
    command_timeout: int = 120
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
        except (TypeError, ValueError):
            raise ValueError(f"Неверное значение для {key}: {value!r}") from None
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} должен быть одним из: {', '.join(CHOICES[key])}")
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
            except (OSError, json.JSONDecodeError):
                data = {}
            for key, value in validate(data).items():
                setattr(s, key, value)
        if use_env and not s.api_key:
            s.api_key = os.environ.get("ANYDESK_HELPER_API_KEY", "")
        return s

    def update(self, values: dict) -> Settings:
        s = self.load(use_env=False)  # ключ из переменной окружения в файл не пишем
        values = dict(values)
        if not values.get("api_key"):
            values.pop("api_key", None)  # пустое поле в форме = не менять ключ
        for key, value in validate(values).items():
            setattr(s, key, value)
        self.save(s)
        return self.load()

    def save(self, s: Settings) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
