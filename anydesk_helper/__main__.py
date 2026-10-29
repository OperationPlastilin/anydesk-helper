"""Точка входа: проверка обновлений -> локальный веб-сервер -> браузер."""

from __future__ import annotations

import argparse
import http.client
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import anydesk, updater
from .config import RANGES, SettingsStore
from .paths import data_dir, is_frozen
from .version import __version__


def port_arg(value: str) -> int:
    low, high = RANGES["port"]
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"порт — это число: {value!r}") from None
    if not low <= port <= high:
        raise argparse.ArgumentTypeError(f"порт должен быть от {low} до {high}")
    return port


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="AnyDeskHelper",
                                description="ИИ-агент техподдержки через AnyDesk с веб-интерфейсом.")
    p.add_argument("--port", type=port_arg, help="порт веб-интерфейса (по умолчанию из настроек, 8765)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.add_argument("--no-update", action="store_true", help="не проверять обновления при запуске")
    p.add_argument("--check", action="store_true", help="проверить модель из настроек и выйти")
    p.add_argument("--dev", action="store_true",
                   help="режим разработчика: можно запускать агента на этом ПК и без нажатий")
    return p.parse_args(argv)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def app_token(data: Path) -> str:
    """Постоянный ключ доступа к веб-интерфейсу. Лежит в папке данных пользователя
    (%LOCALAPPDATA%): другие пользователи этого ПК его не прочитают, а закладки
    со ссылкой ?t=… переживают перезапуск и обновление."""
    path = Path(data) / "token"
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        token = ""
    if len(token) < 24:
        token = secrets.token_urlsafe(24)
        try:
            path.write_text(token, encoding="ascii")
        except OSError:
            pass  # не записали — ключ будет новым при каждом запуске
    return token


def own_instance(port: int, token: str) -> bool:
    """На порту — наша же программа (этого пользователя Windows), а не чужая?"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/state", headers={"X-Token": token})
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):  # не HTTP (например, SSH-баннер) — чужая программа
        return False
    finally:
        conn.close()


def pause_before_exit() -> None:
    """В exe окно консоли закрылось бы сразу — даём прочитать сообщение."""
    if is_frozen():
        try:
            input("Нажмите Enter, чтобы закрыть окно…")
        except (EOFError, OSError, RuntimeError):
            pass


def startup_update(store, argv) -> bool:
    """True — запущено обновление, программа сейчас перезапустится."""
    try:
        if not (store.load().auto_update and updater.can_self_update()):
            return False
        info = updater.check(__version__)
        if not info:
            return False
        print(f"Найдена новая версия {info['version']} — обновляю…", flush=True)
        updater.install(info, restart_args=list(argv), exit_after=None)
        return True
    except Exception as e:  # обновление ни при какой ошибке не должно мешать запуску
        print(f"Обновление пропущено: {e}", flush=True)
        return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    anydesk.enable_dpi_awareness()
    anydesk.set_console_title("AnyDesk Helper")
    data = data_dir()
    store = SettingsStore(data / "settings.json")
    print(f"AnyDesk Helper {__version__}\nДанные: {data}", flush=True)

    port = args.port or store.load().port
    # Порт занят — скорее всего, программа уже запущена: её exe не подменить, обновится она
    # сама из интерфейса. Поэтому не обновляемся, а открываем работающую программу.
    busy = port_in_use(port)
    if not args.no_update and not busy and startup_update(store, argv):
        return 0

    if args.check:
        from .checks import check_model
        from .llm import LLM

        s = store.load()
        ok, report = check_model(LLM(s), s)
        print("\n".join(report))
        return 0 if ok else 1

    token = app_token(data)
    url = f"http://127.0.0.1:{port}/?t={token}"
    if busy:
        if not own_instance(port, token):
            print(f"Порт {port} занят другой программой или другим пользователем Windows.\n"
                  f"Запустите с другим портом: AnyDeskHelper.exe --port {port + 1}", flush=True)
            pause_before_exit()
            return 1
        print(f"Программа уже запущена. Открываю {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    from waitress import serve

    from .db import DB
    from .knowledge import KnowledgeBase
    from .session import SessionManager
    from .web import create_app

    db = DB(data / "tickets.db")
    kb = KnowledgeBase(db)
    manager = SessionManager(db, store, data, kb=kb)
    app = create_app(db, store, manager, port, dev=args.dev, token=token, kb=kb)
    print(f"Веб-интерфейс: {url}\nНе закрывайте это окно, пока работаете. Ctrl+C — выход.", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        serve(app, host="127.0.0.1", port=port, threads=8, ident="AnyDeskHelper")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
