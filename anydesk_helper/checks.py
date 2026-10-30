"""Проверка модели: отвечает ли, держит ли формат JSON и видит ли картинки."""

from __future__ import annotations

from PIL import Image, ImageDraw

from .actions import ActionError, extract_json
from .llm import LLMError, user_message
from .screen import Frame, Region


def _not_cut(llm) -> None:
    """Обрезанный по max_tokens ответ мог кончиться черновым JSON из рассуждений — это не успех."""
    if getattr(llm, "finish_reason", "") == "length":
        raise ActionError("ответ обрезан по max_tokens — увеличьте max_tokens в настройках "
                          "или выберите модель без thinking")


def check_model(llm, settings) -> tuple[bool, list[str]]:
    report = [f"Модель: {settings.model} ({settings.base_url})"]
    try:
        reply = llm.chat([{"role": "user", "content":
                           'Reply with JSON only: {"status": "ok", "language": "<your name>"}'}])
        _not_cut(llm)
        extract_json(reply)
        report.append("✔ Модель отвечает и соблюдает формат JSON")
    except (LLMError, ActionError) as e:
        report.append(f"✘ Текстовый запрос: {e}")
        return False, report
    if not settings.vision:
        report.append("Скриншоты выключены (vision = нет): агент будет работать только через консоль")
        return True, report

    # Красный квадрат на белом фоне — проверяем зрение и формат координат.
    img = Image.new("RGB", (560, 336), "white")
    ImageDraw.Draw(img).rectangle((364, 196, 448, 280), fill=(220, 0, 0))
    frame = Frame(img, Region(0, 0, 560, 336))
    unit = "pixels of this 560x336 image" if settings.coords == "pixels" else "0..1000 normalized units"
    question = ("Where is the center of the red square? Reply with JSON only: "
                f'{{"x": .., "y": ..}} (coordinates in {unit}).')
    try:
        reply = llm.chat([user_message(question, frame.to_data_url())])
        _not_cut(llm)
        data = extract_json(reply)
        x, y = float(data["x"]), float(data["y"])
    except (LLMError, ActionError, KeyError, TypeError, ValueError) as e:
        report.append(f"✘ Картинки: модель не ответила координатами ({e}). Если модель не "
                      "умеет видеть изображения — выключите «Скриншоты» в настройках.")
        return False, report
    if settings.coords == "norm1000":
        x, y = x / 1000 * 560, y / 1000 * 336
    err = ((x - 406) ** 2 + (y - 238) ** 2) ** 0.5
    if err > 40:
        other = "norm1000" if settings.coords == "pixels" else "pixels"
        report.append(f"✘ Картинки: промах {err:.0f} px. Попробуйте формат координат «{other}» "
                      "или другую модель.")
        return False, report
    report.append(f"✔ Модель видит скриншоты, точность {err:.0f} px")
    return True, report
