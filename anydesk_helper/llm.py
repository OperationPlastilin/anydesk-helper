"""Клиент для OpenAI-совместимого API (OpenRouter, DashScope и любые другие)."""

from __future__ import annotations

import time

import requests


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings, session: requests.Session | None = None):
        self.s = settings
        self.base = settings.base_url.rstrip("/")
        self.http = session or requests.Session()
        self.json_mode = settings.json_mode

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   # OpenRouter показывает эти поля в статистике; другим провайдерам не мешают.
                   "HTTP-Referer": "https://github.com/OperationPlastilin/anydesk-helper",
                   "X-Title": "AnyDesk Helper"}
        if self.s.api_key:
            headers["Authorization"] = f"Bearer {self.s.api_key}"
        return headers

    def chat(self, messages: list[dict]) -> str:
        if not self.base:
            raise LLMError("Не указан адрес API (Настройки -> Модель)")
        payload = {
            "model": self.s.model,
            "messages": messages,
            "temperature": self.s.temperature,
            "max_tokens": self.s.max_tokens,
        }
        last_error = ""
        for attempt in range(4):
            if self.json_mode:
                payload["response_format"] = {"type": "json_object"}
            else:
                payload.pop("response_format", None)
            try:
                resp = self.http.post(self.base + "/chat/completions", json=payload,
                                      headers=self._headers(), timeout=self.s.timeout)
            except requests.RequestException as e:
                last_error = f"нет связи с {self.base}: {e}"
            else:
                if resp.status_code == 200:
                    return self._content(resp)
                last_error = f"HTTP {resp.status_code}: {resp.text[:400]}"
                # Не все модели поддерживают JSON-режим — тогда просто выключаем его.
                if resp.status_code in (400, 422) and self.json_mode and "response_format" in resp.text:
                    self.json_mode = False
                    continue
                if resp.status_code in (401, 403):
                    raise LLMError(f"Неверный или пустой API-ключ ({last_error})")
                # 429 — лимит бесплатного тарифа, 5xx — сервер занят: имеет смысл подождать.
                if resp.status_code != 429 and resp.status_code < 500:
                    break
            time.sleep(5 * (attempt + 1))
        raise LLMError(last_error)

    @staticmethod
    def _content(resp) -> str:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(f"Неожиданный ответ API: {resp.text[:400]}") from None
        if isinstance(content, list):  # некоторые провайдеры возвращают список частей
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""

    def list_models(self) -> list[dict]:
        """Список моделей провайдера (для выбора в настройках). Qwen — первыми."""
        try:
            resp = self.http.get(self.base + "/models", headers=self._headers(), timeout=30)
            resp.raise_for_status()
            items = resp.json().get("data", [])
        except (requests.RequestException, ValueError, AttributeError) as e:
            raise LLMError(f"Не удалось получить список моделей: {e}") from None
        models = []
        for m in items:
            mid = m.get("id", "")
            arch = m.get("architecture") or {}
            modalities = arch.get("input_modalities") or []
            pricing = m.get("pricing") or {}
            models.append({
                "id": mid,
                "vision": "image" in modalities or "vl" in mid.lower(),
                "free": mid.endswith(":free") or (pricing and all(
                    str(v) in ("0", "0.0") for v in (pricing.get("prompt"), pricing.get("completion")))),
            })
        models.sort(key=lambda m: ("qwen" not in m["id"].lower(), not m["vision"], not m["free"], m["id"]))
        return models


def user_message(text: str, image_data_url: str | None = None) -> dict:
    if not image_data_url:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]}
