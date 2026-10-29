"""Клиент для OpenAI-совместимого API (OpenRouter, DashScope и любые другие)."""

from __future__ import annotations

import time
from datetime import datetime

import requests

MAX_TOKENS_CAP = 8192  # до этого предела клиент сам поднимает max_tokens для thinking-моделей


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings, session: requests.Session | None = None, sleep=time.sleep):
        self.s = settings
        self.base = settings.base_url.rstrip("/")
        self.http = session or requests.Session()
        self.json_mode = settings.json_mode
        self.model = settings.model
        self.max_tokens = settings.max_tokens
        self.sleep = sleep
        self.should_stop = lambda: False  # сеанс подставляет «нажат Стоп»
        self.notify = lambda text: None  # сеанс может показать технику смену модели или max_tokens
        self.finish_reason = ""  # причина конца последнего ответа ("length" — обрезан по max_tokens)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   # OpenRouter показывает эти поля в статистике; другим провайдерам не мешают.
                   "HTTP-Referer": "https://github.com/OperationPlastilin/anydesk-helper",
                   "X-Title": "AnyDesk Helper"}
        if self.s.api_key:
            headers["Authorization"] = f"Bearer {self.s.api_key}"
        return headers

    def _check_stop(self) -> None:
        if self.should_stop():
            raise LLMError("Остановлено техником")

    def _wait(self, seconds: float) -> None:
        """Пауза между попытками: «Стоп» прерывает её сразу."""
        while seconds > 0:
            self._check_stop()
            self.sleep(min(seconds, 0.5))
            seconds -= 0.5
        self._check_stop()

    def chat(self, messages: list[dict]) -> str:
        if not self.base:
            raise LLMError("Не указан адрес API (Настройки -> Модель)")
        payload = {"messages": messages, "temperature": self.s.temperature}
        last_error = ""
        for attempt in range(4):
            self._check_stop()
            payload.update(model=self.model, max_tokens=self.max_tokens)
            if self.json_mode:
                payload["response_format"] = {"type": "json_object"}
            else:
                payload.pop("response_format", None)
            wait = 5 * (attempt + 1)
            try:
                resp = self.http.post(self.base + "/chat/completions", json=payload,
                                      headers=self._headers(), timeout=(15, self.s.timeout))
            except requests.RequestException as e:
                if isinstance(e, ValueError):  # MissingSchema, InvalidURL…: повтор не поможет
                    raise LLMError(f"Неверный адрес API {self.base} — он должен начинаться "
                                   f"с https:// ({e})") from None
                last_error = f"нет связи с {self.base}: {e}"
            else:
                if resp.status_code == 200:
                    content, self.finish_reason = self._content(resp)
                    if content and self.finish_reason == "length" and self.max_tokens < MAX_TOKENS_CAP:
                        # Обрезанный ответ отдаём с finish_reason="length" (агент его не выполняет и
                        # переспрашивает), а для следующего запроса поднимаем лимит.
                        self.max_tokens = min(max(self.max_tokens * 2, 4096), MAX_TOKENS_CAP)
                        self.notify(f"Ответ модели обрезан по max_tokens — лимит увеличен до {self.max_tokens}")
                    if content or self.finish_reason != "length":
                        return content
                    # Пустой ответ: thinking-модель потратила весь лимит на рассуждения.
                    last_error = (f"модель израсходовала max_tokens={self.max_tokens} (вероятно, на "
                                  "рассуждения thinking-модели) и не дала ответа. Увеличьте max_tokens "
                                  "в настройках или выберите модель без thinking")
                    if self.max_tokens >= MAX_TOKENS_CAP:
                        break
                    self.max_tokens = min(max(self.max_tokens * 2, 4096), MAX_TOKENS_CAP)
                    self.notify(f"Модель не уложилась в лимит ответа — max_tokens увеличен до {self.max_tokens}")
                    continue
                last_error = f"HTTP {resp.status_code}: {resp.text[:400]}"
                # Не все модели поддерживают JSON-режим — тогда просто выключаем его.
                if resp.status_code in (400, 422) and self.json_mode and "response_format" in resp.text:
                    self.json_mode = False
                    continue
                if resp.status_code in (401, 403):
                    raise LLMError(f"Неверный или пустой API-ключ ({last_error})")
                if resp.status_code == 429:
                    daily, delay, reset = self._rate_limit(resp)
                    fallback = (getattr(self.s, "fallback_model", "") or "").strip()
                    if daily and fallback and fallback != self.model:
                        self.notify(f"Дневной лимит модели {self.model} исчерпан — переключаюсь "
                                    f"на запасную модель {fallback}")
                        self.model = fallback
                        continue
                    if daily:
                        raise LLMError(
                            "Исчерпан дневной лимит бесплатных запросов"
                            + (f" (сброс около {reset:%H:%M})" if reset else "")
                            + ". Повтор сейчас не поможет: пополните баланс OpenRouter ($10 — 1000 "
                            "бесплатных запросов в день), укажите «Запасную модель» или выберите другого "
                            "провайдера (DashScope) в Настройках.")
                    if delay is not None and delay > 60:
                        raise LLMError(f"Лимит запросов провайдера: повторить можно через "
                                       f"{delay / 60:.0f} мин ({last_error})")
                    wait = delay if delay is not None else wait
                # 5xx — сервер занят: имеет смысл подождать; прочие ошибки повтором не исправить.
                elif resp.status_code < 500:
                    break
            if attempt < 3:
                self._wait(wait)
        raise LLMError(last_error)

    @staticmethod
    def _rate_limit(resp) -> tuple[bool, float | None, datetime | None]:
        """429: дневной ли это лимит, сколько ждать (сек, если провайдер сказал) и когда сброс.
        OpenRouter кладёт заголовки лимита в error.metadata.headers; X-RateLimit-Reset — мс эпохи."""
        try:
            meta = (resp.json().get("error") or {}).get("metadata") or {}
            headers = dict(meta.get("headers") or {})
        except (ValueError, AttributeError, TypeError):
            headers = {}

        def header(name):
            return headers.get(name) or resp.headers.get(name)

        delay = reset = None
        try:
            delay = float(header("Retry-After"))
        except (TypeError, ValueError):
            pass
        try:
            reset = datetime.fromtimestamp(int(header("X-RateLimit-Reset")) / 1000)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        until_reset = (reset - datetime.now()).total_seconds() if reset else None
        text = resp.text.lower()
        daily = ("per-day" in text or "per day" in text
                 or (str(header("X-RateLimit-Remaining")) == "0" and (until_reset or 0) > 120))
        if delay is None and until_reset is not None and until_reset > 0:
            delay = until_reset
        return daily, delay, reset

    @staticmethod
    def _content(resp) -> tuple[str, str]:
        """Текст ответа и finish_reason."""
        try:
            choice = resp.json()["choices"][0]
            content = choice["message"]["content"]
            finish = str(choice.get("finish_reason") or "")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"Неожиданный ответ API: {resp.text[:400]}") from None
        if isinstance(content, list):  # некоторые провайдеры возвращают список частей
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or "", finish

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
                # Цены не пришли (DashScope, свой сервер) — значит, неизвестно, а не «бесплатно».
                "free": mid.endswith(":free") or bool(pricing) and all(
                    _zero(pricing.get(k)) for k in ("prompt", "completion")),
            })
        models.sort(key=lambda m: ("qwen" not in m["id"].lower(), not m["vision"], not m["free"], m["id"]))
        return models


def _zero(value) -> bool:
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def user_message(text: str, image_data_url: str | None = None) -> dict:
    if not image_data_url:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]}
