"""Точка входа: python -m anydesk_helper --id 123456789 --task "установить MS Office"."""

from __future__ import annotations

import argparse
import sys

from PIL import Image, ImageDraw

from . import anydesk
from .actions import ActionError, extract_json
from .agent import Agent, Console, SessionLog
from .config import load_config
from .controller import Controller
from .llm import LLM, LLMError, user_message
from .screen import Frame, Region


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="anydesk_helper",
        description="ИИ-агент техподдержки: подключается по AnyDesk и решает задачу "
                    "мышью, клавиатурой и через консоль.")
    p.add_argument("--id", help="ID (или алиас) удалённого AnyDesk")
    p.add_argument("--password", help="пароль неконтролируемого доступа (необязательно)")
    p.add_argument("--task", help="что нужно сделать, например «установить Microsoft Office»")
    p.add_argument("--config", default="config.toml", help="файл настроек (по умолчанию config.toml)")
    p.add_argument("--model", help="переопределить модель из конфига")
    p.add_argument("--base-url", help="переопределить адрес API из конфига")
    p.add_argument("--coords", choices=["pixels", "norm1000"], help="формат координат модели")
    p.add_argument("--confirm", choices=["all", "risky", "none"],
                   help="какие действия подтверждать вручную (по умолчанию risky)")
    p.add_argument("--no-connect", action="store_true",
                   help="не запускать AnyDesk: сеанс уже открыт, просто найти окно")
    p.add_argument("--local", action="store_true",
                   help="управлять ЭТИМ компьютером без AnyDesk (для проверки)")
    p.add_argument("--dry-run", action="store_true",
                   help="ничего не нажимать, только показывать, что сделал бы агент")
    p.add_argument("--check", action="store_true",
                   help="проверить, что модель доступна и видит картинки, и выйти")
    return p.parse_args(argv)


def check_model(llm: LLM, coords: str) -> bool:
    """Рисуем красный квадрат и просим модель назвать цвет и центр — проверка зрения."""
    img = Image.new("RGB", (560, 336), "white")
    ImageDraw.Draw(img).rectangle((364, 196, 448, 280), fill=(220, 0, 0))
    frame = Frame(img, Region(0, 0, 560, 336))
    unit = "pixels of the 560x336 image" if coords == "pixels" else "0..1000 normalized units"
    question = ("What color is the square and where is its center? Reply with JSON only: "
                f'{{"color": "...", "x": .., "y": ..}} (coordinates in {unit}).')
    print(f"Запрос к {llm.cfg.model} ({llm.url})...")
    reply = llm.chat([user_message(question, frame.to_data_url())])
    print(f"Ответ модели: {reply.strip()[:300]}")
    try:
        data = extract_json(reply)
        x, y = float(data["x"]), float(data["y"])
    except (ActionError, KeyError, TypeError, ValueError):
        print("Модель отвечает, но не в нужном формате. Попробуйте модель покрупнее.")
        return False
    if coords == "norm1000":
        x, y = x / 1000 * 560, y / 1000 * 336
    err = ((x - 406) ** 2 + (y - 238) ** 2) ** 0.5
    print(f"Центр квадрата: (406, 238), модель указала ({x:.0f}, {y:.0f}), промах {err:.0f} px")
    if err > 40:
        print("Промах большой: проверьте параметр coords (pixels / norm1000) или смените модель.")
        return False
    print("OK: модель видит изображение и правильно указывает координаты.")
    return True


def choose_target(args, cfg, console: Console):
    if args.local:
        console.say("ВНИМАНИЕ: агент будет управлять этим компьютером. "
                    "Аварийная остановка — увести мышь в левый верхний угол экрана.")
        return anydesk.ScreenTarget(), "local"

    address = args.id or console.ask("ID AnyDesk удалённого компьютера: ")
    address = anydesk.normalize_address(address)
    if not address:
        raise SystemExit("Не указан ID AnyDesk")
    if not args.no_connect:
        exe = anydesk.find_executable(cfg.anydesk.path)
        console.say(f"Подключаюсь к {address} ...")
        anydesk.connect(exe, address, args.password)
        console.say("Ждём, пока пользователь примет подключение в AnyDesk...")
    window = anydesk.wait_for_session(address, 5 if args.no_connect else cfg.anydesk.connect_timeout,
                                      cfg.anydesk.window_title)
    if not window:
        console.ask("Окно сеанса не найдено автоматически. Нажмите Enter и в течение 5 секунд "
                    "кликните по окну сеанса AnyDesk.")
        window = anydesk.pick_active_window()
    if not window:
        raise SystemExit("Не удалось определить окно сеанса AnyDesk (поддерживается Windows).")
    console.say(f"Окно сеанса: «{window.title}». Совет: разверните его на весь экран.")
    return anydesk.WindowTarget(window), "anydesk"


def main(argv=None) -> int:
    args = parse_args(argv)
    anydesk.enable_dpi_awareness()
    anydesk.set_console_title("AI Helper")
    cfg = load_config(args.config)
    if args.model:
        cfg.llm.model = args.model
    if args.base_url:
        cfg.llm.base_url = args.base_url
    if args.coords:
        cfg.llm.coords = args.coords
    if args.confirm:
        cfg.agent.confirm = args.confirm
    llm = LLM(cfg.llm)

    try:
        if args.check:
            return 0 if check_model(llm, cfg.llm.coords) else 1

        console = Console()
        target, surface = choose_target(args, cfg, console)
        task = args.task or console.ask("Опишите задачу (например: «установить Microsoft Office»): ")
        if not task:
            raise SystemExit("Задача не указана")
        agent = Agent(cfg, llm, target, Controller(cfg.agent.typing, args.dry_run),
                      console, SessionLog(cfg.agent.log_dir), surface=surface)
        console.say("Управление: Ctrl+C — пауза/подсказка агенту; мышь в левый верхний угол "
                    "экрана — аварийная остановка.")
        return 0 if agent.run(task) else 1
    except LLMError as e:
        print(f"Ошибка обращения к модели: {e}")
        print("Проверьте, запущен ли Ollama / верны ли base_url, model и api_key в config.toml.")
        return 2
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
