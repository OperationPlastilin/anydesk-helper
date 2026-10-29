"""Точка входа: проверка обновлений -> локальный веб-сервер -> браузер."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from . import anydesk, updater
from .config import SettingsStore
from .paths import data_dir
from .version import __version__


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="AnyDeskHelper",
                                description="ИИ-агент техподдержки через AnyDesk с веб-интерфейсом.")
    p.add_argument("--port", type=int, help="порт веб-интерфейса (по умолчанию из настроек, 8765)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.add_argument("--no-update", action="store_true", help="не проверять обновления при запуске")
    p.add_argument("--check", action="store_true", help="проверить модель из настроек и выйти")
    p.add_argument("--dev", action="store_true",
                   help="режим разработчика: можно запускать агента на этом ПК и без нажатий")
    return p.parse_args(argv)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def startup_update(store, argv) -> bool:
    """True — запущено обновление, программа сейчас перезапустится."""
    if not (store.load().auto_update and updater.can_self_update()):
        return False
    try:
        info = updater.check(__version__)
        if not info:
            return False
        print(f"Найдена новая версия {info['version']} — обновляю…", flush=True)
        updater.install(info, restart_args=list(argv), exit_after=None)
        return True
    except updater.UpdateError as e:
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

    if not args.no_update and startup_update(store, argv):
        return 0

    if args.check:
        from .checks import check_model
        from .llm import LLM

        s = store.load()
        ok, report = check_model(LLM(s), s)
        print("\n".join(report))
        return 0 if ok else 1

    port = args.port or store.load().port
    url = f"http://127.0.0.1:{port}/"
    if port_in_use(port):
        print(f"Порт {port} занят — похоже, программа уже запущена. Открываю {url}")
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
    app = create_app(db, store, manager, port, dev=args.dev, kb=kb)
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
