"""Скриншоты окна AnyDesk и перевод координат скриншота в координаты экрана."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

# Qwen-VL режет картинку на патчи 28x28 px. Если стороны кратны 28, модель видит
# изображение без дополнительного масштабирования и её пиксельные координаты точнее.
PATCH = 28


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int


@dataclass
class Frame:
    """Скриншот в том виде, в каком его видит модель, плюс откуда он снят."""

    image: Image.Image
    region: Region

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        w, h = self.image.size
        sx = self.region.left + x * self.region.width / w
        sy = self.region.top + y * self.region.height / h
        # Не даём кликнуть за пределами окна сеанса.
        sx = min(max(sx, self.region.left), self.region.left + self.region.width - 1)
        sy = min(max(sy, self.region.top), self.region.top + self.region.height - 1)
        return round(sx), round(sy)

    def to_data_url(self, quality: int = 80) -> str:
        buf = io.BytesIO()
        self.image.convert("RGB").save(buf, format="JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def fit_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    scale = min(1.0, max_side / max(width, height))
    w = max(PATCH, round(width * scale / PATCH) * PATCH)
    h = max(PATCH, round(height * scale / PATCH) * PATCH)
    return w, h


def make_frame(raw: Image.Image, region: Region, max_side: int) -> Frame:
    size = fit_size(raw.width, raw.height, max_side)
    image = raw if raw.size == size else raw.resize(size, Image.LANCZOS)
    return Frame(image=image, region=region)


def grab(region: Region) -> Image.Image:
    import mss

    with mss.mss() as sct:
        shot = sct.grab({"left": region.left, "top": region.top,
                         "width": region.width, "height": region.height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def virtual_screen() -> Region:
    import mss

    with mss.mss() as sct:
        m = sct.monitors[0]
        return Region(m["left"], m["top"], m["width"], m["height"])


def primary_monitor() -> Region:
    import mss

    with mss.mss() as sct:
        m = sct.monitors[1]
        return Region(m["left"], m["top"], m["width"], m["height"])


def annotate(image: Image.Image, points: list[tuple[float, float]]) -> Image.Image:
    """Рисует точки кликов на копии скриншота — для логов."""
    img = image.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    for x, y in points:
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), outline=(255, 0, 0), width=3)
        draw.line((x - 16, y, x + 16, y), fill=(255, 0, 0), width=2)
        draw.line((x, y - 16, x, y + 16), fill=(255, 0, 0), width=2)
    return img
