"""Удалённый ввод: фокус окна AnyDesk, раскладка клавиатуры, поиск окна сеанса."""

import sys
import types

import pytest

from anydesk_helper import anydesk
from anydesk_helper.actions import Action
from anydesk_helper.anydesk import FocusError, WindowTarget, find_session_window
from anydesk_helper.controller import Controller, layout_independent_keys
from anydesk_helper.screen import Frame, Region

from .fakes import Clock, FakeClipboard, FakeTarget, fake_grab


class FakeGui:
    """pyautogui: записывает нажатия и клики в общий журнал."""

    def __init__(self, log):
        self.log = log

    def press(self, key):
        self.log.append(("press", key))

    def hotkey(self, *keys, interval=0.0):
        self.log.append(("hotkey",) + keys)

    def moveTo(self, x, y, duration=0.0):
        self.log.append(("move", x, y))

    def click(self, x, y, button="left"):
        self.log.append(("click", x, y))


def make_controller(**target_kw):
    log = []
    target = FakeTarget(log=log, **target_kw)
    c = Controller(target=target, clipboard=FakeClipboard(), sleep=Clock().sleep)
    c._gui = FakeGui(log)
    return c, target, log


def test_open_terminal_focuses_before_every_key():
    c, _, log = make_controller()
    c.open_terminal(admin=True)
    assert log == ["focus", ("hotkey", "win", "r"), "focus", ("hotkey", "ctrl", "v"),
                   "focus", ("hotkey", "ctrl", "shift", "enter")]
    assert c.clipboard.text == "powershell"


def test_no_input_when_focus_fails():
    # Win+R не должен открыть «Выполнить» на компьютере техника
    c, _, log = make_controller(fail_focus=1)
    with pytest.raises(FocusError):
        c.open_terminal(admin=False)
    assert log == []
    c, _, log = make_controller()
    c.paste_line("Get-Date")
    assert log == ["focus", ("hotkey", "ctrl", "v"), "focus", ("press", "enter")]


def test_gui_actions_focus_first_and_refuse_stale_coordinates():
    c, target, log = make_controller()
    frame = Frame(fake_grab(target.region()), target.region())
    c.execute(Action("click", x=10, y=20), frame)
    assert log == ["focus", ("move", 10, 20), ("click", 10, 20)]
    c.execute(Action("type", text="привет", enter=True), frame)
    assert log[3:] == ["focus", ("hotkey", "ctrl", "v"), "focus", ("press", "enter")]
    target.rect = Region(300, 200, 1288, 728)  # окно сдвинули, пока думала модель
    del log[:]
    with pytest.raises(RuntimeError, match="moved"):
        c.execute(Action("click", x=10, y=20), frame)
    assert log == ["focus"]  # не кликнули мимо окна


def test_dry_run_touches_nothing():
    c = Controller(dry_run=True, target=FakeTarget(fail_focus=5))
    c.open_terminal(admin=True)
    c.paste_line("x")
    assert c.execute(Action("key", keys=["enter"]), None).startswith("dry-run")


def test_layout_independent_keys():
    # VkKeyScanA в русской раскладке даёт -1 для латиницы — Ctrl+V превращался в мусор
    mapping = {"v": -1, "r": 0xFFFF, "c": -1, "enter": 0x0D, "1": 0x31, ";": -1, "?": 0x1BF}
    layout_independent_keys(mapping)
    assert (mapping["v"], mapping["r"], mapping["c"], mapping["V"]) == (0x56, 0x52, 0x43, 0x56)
    assert mapping["enter"] == 0x0D and mapping["1"] == 0x31 and mapping["?"] == 0x1BF
    assert mapping[";"] is None  # не Ctrl+Alt+Shift+0xFF, а пропуск


def test_gui_import_fixes_key_mapping_on_windows(monkeypatch):
    mapping = {"v": -1}
    fake = types.ModuleType("pyautogui")
    fake.platformModule = types.SimpleNamespace(keyboardMapping=mapping)
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert Controller().gui is fake and mapping["v"] == 0x56 and fake.FAILSAFE is True


class FakeUser32:
    """user32: окно становится активным после `works_after` вызовов SetForegroundWindow."""

    def __init__(self, hwnd=42, works_after=1, alive=True):
        self.fg, self.hwnd, self.works_after, self.alive = 7, hwnd, works_after, alive
        self.calls = []

    def IsWindow(self, h):
        return h in self.alive if isinstance(self.alive, set) else self.alive

    def GetForegroundWindow(self):
        return self.fg

    def IsIconic(self, h):
        return False

    def ShowWindow(self, h, cmd):
        self.calls.append("restore")

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(("alt", flags))

    def SetForegroundWindow(self, h):
        self.calls.append("set")
        self.works_after -= 1
        if self.works_after == 0:
            self.fg = h
        return int(self.fg == h)


def window_target(monkeypatch, user32):
    monkeypatch.setattr(anydesk, "_user32", lambda: user32)
    win = types.SimpleNamespace(_hWnd=user32.hwnd, title="123 456 789 - AnyDesk")
    return WindowTarget(win, sleep=Clock().sleep)


def test_focus_verifies_foreground(monkeypatch):
    u = FakeUser32(works_after=1)
    window_target(monkeypatch, u).focus()
    assert u.calls == ["set"]
    u.calls = []
    window_target(monkeypatch, u).focus()  # уже впереди — ничего не нажимаем
    assert u.calls == []


def test_focus_uses_alt_unlock_then_gives_up(monkeypatch):
    u = FakeUser32(works_after=2)
    window_target(monkeypatch, u).focus()
    # Alt нажат и отпущен ДО SetForegroundWindow — отпускание не уйдёт через AnyDesk
    assert u.calls == ["set", ("alt", 0), ("alt", anydesk.KEYEVENTF_KEYUP), "set"]
    with pytest.raises(FocusError):
        window_target(monkeypatch, FakeUser32(works_after=99)).focus()
    with pytest.raises(FocusError, match="закрыто"):
        window_target(monkeypatch, FakeUser32(alive=False)).focus()


def test_session_window_never_matches_our_own_windows(monkeypatch):
    titles = ["AnyDesk Helper - Google Chrome", "AnyDesk Helper", "AnyDesk", "Office-PC - AnyDesk",
              "123 456 789 - AnyDesk"]
    wins = [types.SimpleNamespace(title=t) for t in titles]
    monkeypatch.setattr(anydesk, "_windows", lambda: wins)
    # «AnyDesk» подходит к любому окну AnyDesk — ищем по ID
    assert find_session_window("123 456 789", "AnyDesk").title == "123 456 789 - AnyDesk"
    assert find_session_window("123 456 789", " anydesk ").title == "123 456 789 - AnyDesk"
    assert find_session_window("123 456 789", "office").title == "Office-PC - AnyDesk"
    assert find_session_window("123 456 789", "helper") is None
    assert find_session_window("123456789").title == "123 456 789 - AnyDesk"


def test_focus_lost_before_enter_clears_console_line_on_retry():
    # Обёртка вставлена, а Enter не нажат: повтор не должен дописать вторую обёртку к первой
    from anydesk_helper.bridge import ConsoleBridge

    log, clock = [], Clock()
    target = FakeTarget(log=log, fail_on={3})  # 1 — бридж, 2 — перед Ctrl+V, 3 — перед Enter
    c = Controller(target=target, clipboard=FakeClipboard(), sleep=clock.sleep)
    c._gui = FakeGui(log)
    bridge = ConsoleBridge(c, target, clipboard=c.clipboard, sleep=clock.sleep, clock=clock)
    with pytest.raises(FocusError):
        bridge.run("Get-Date")
    assert log == ["focus", "focus", ("hotkey", "ctrl", "v")] and bridge.pending is None
    del log[:]
    assert bridge.run("Get-Date").status == "no_ack"  # в фейке никто не отвечает
    assert log == ["focus", "focus", ("press", "esc"), "focus", ("hotkey", "ctrl", "v"),
                   "focus", ("press", "enter")]
    del log[:]
    c.paste_line("Get-Date")  # строка ввода чистая — Esc больше не шлём
    assert ("press", "esc") not in log


def test_no_esc_when_nothing_was_pasted():
    c, _, log = make_controller(fail_focus=1)  # упал фокус перед Ctrl+V — строка пустая
    with pytest.raises(FocusError):
        c.paste_line("Get-Date")
    c.paste_line("Get-Date")
    assert log == ["focus", ("hotkey", "ctrl", "v"), "focus", ("press", "enter")]


def test_type_is_not_retried_after_paste():
    # FocusError перед Enter -> агент повторил бы execute и вставил текст второй раз
    c, target, log = make_controller(fail_on={2})
    frame = Frame(fake_grab(target.region()), target.region())
    with pytest.raises(RuntimeError, match="Enter was NOT pressed") as e:
        c.execute(Action("type", text="привет", enter=True), frame)
    assert not isinstance(e.value, FocusError)
    assert log == ["focus", ("hotkey", "ctrl", "v")]


def test_closed_session_window_is_rebound_after_reconnect(monkeypatch):
    u = FakeUser32(hwnd=42, alive={43})  # старое окно 42 закрыто, AnyDesk открыл новое 43
    target = window_target(monkeypatch, u)
    other = types.SimpleNamespace(_hWnd=44, title="Office-PC - AnyDesk")
    new = types.SimpleNamespace(_hWnd=43, title="123 456 789 - AnyDesk")
    monkeypatch.setattr(anydesk, "_windows", lambda: [other, new])
    target.focus()
    assert target.window is new and u.fg == 43
    # «AnyDesk» — общий заголовок (и главное окно AnyDesk у техника): не перепривязываем
    u = FakeUser32(hwnd=42, alive={43})
    target = window_target(monkeypatch, u)
    target.title = "AnyDesk"
    monkeypatch.setattr(anydesk, "_windows", lambda: [types.SimpleNamespace(_hWnd=43, title="AnyDesk")])
    with pytest.raises(FocusError, match="закрыто"):
        target.focus()
    # два окна с тем же заголовком — неоднозначно
    u = FakeUser32(hwnd=42, alive={43, 45})
    target = window_target(monkeypatch, u)
    monkeypatch.setattr(anydesk, "_windows", lambda: [
        types.SimpleNamespace(_hWnd=h, title="123 456 789 - AnyDesk") for h in (43, 45)])
    with pytest.raises(FocusError, match="закрыто"):
        target.focus()
