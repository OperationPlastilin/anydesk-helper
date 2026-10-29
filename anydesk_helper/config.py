"""Загрузка настроек из config.toml (все поля необязательны)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class LLMConfig:
    # Любой OpenAI-совместимый сервер: Ollama, LM Studio, OpenRouter, Alibaba DashScope...
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5vl:7b"
    api_key: str = ""
    # "pixels"   — модель отвечает пикселями скриншота (Qwen2.5-VL)
    # "norm1000" — координаты 0..1000 относительно ширины/высоты (Qwen3-VL)
    coords: str = "pixels"
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout: int = 180


@dataclass
class AnyDeskConfig:
    path: str = ""  # пусто — искать автоматически
    connect_timeout: int = 90  # сколько ждать, пока клиент примет подключение
    window_title: str = ""  # подстрока заголовка окна сеанса; пусто — по ID


@dataclass
class AgentConfig:
    max_steps: int = 60
    confirm: str = "risky"  # all | risky | none
    language: str = "Russian"
    screenshot_max_side: int = 1288
    step_delay: float = 1.5
    typing: str = "clipboard"  # clipboard | keys
    history: int = 15
    log_dir: str = "logs"
    playbooks_dir: str = "playbooks"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    anydesk: AnyDeskConfig = field(default_factory=AnyDeskConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def _fill(section, values: dict, name: str) -> None:
    known = {f.name: f.type for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Неизвестный параметр [{name}] {key} в конфиге")
        current = getattr(section, key)
        if isinstance(current, float) and isinstance(value, int):
            value = float(value)
        setattr(section, key, value)


def load_config(path: str | os.PathLike | None) -> Config:
    cfg = Config()
    if path and Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for name in ("llm", "anydesk", "agent"):
            _fill(getattr(cfg, name), data.get(name, {}), name)
    if not cfg.llm.api_key:
        cfg.llm.api_key = os.environ.get("ANYDESK_HELPER_API_KEY", "")
    if cfg.llm.coords not in ("pixels", "norm1000"):
        raise ValueError("llm.coords должен быть 'pixels' или 'norm1000'")
    if cfg.agent.confirm not in ("all", "risky", "none"):
        raise ValueError("agent.confirm должен быть all, risky или none")
    if cfg.agent.typing not in ("clipboard", "keys"):
        raise ValueError("agent.typing должен быть clipboard или keys")
    return cfg
