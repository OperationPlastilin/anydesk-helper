"""Клиент для любого OpenAI-совместимого API (Ollama, LM Studio, OpenRouter, DashScope)."""

from __future__ import annotations

import time

import requests

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages: list[dict]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        last_error = ""
        for attempt in range(4):
            try:
                resp = requests.post(self.url, json=payload, headers=headers,
                                     timeout=self.cfg.timeout)
            except requests.RequestException as e:
                last_error = f"нет связи с {self.url}: {e}"
            else:
                if resp.status_code == 200:
                    return self._content(resp.json())
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                # 429 — лимит бесплатного тарифа, 5xx — сервер занят: имеет смысл подождать.
                if resp.status_code != 429 and resp.status_code < 500:
                    break
            time.sleep(5 * (attempt + 1))
        raise LLMError(last_error)

    @staticmethod
    def _content(data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Неожиданный ответ API: {str(data)[:500]}") from None
        if isinstance(content, list):  # некоторые провайдеры возвращают список частей
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content or ""


def user_message(text: str, image_data_url: str | None = None) -> dict:
    if not image_data_url:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]}
