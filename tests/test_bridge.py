import base64
import re

from anydesk_helper.bridge import (END, START, ConsoleBridge, enduser_dialog, parse_clipboard,
                                   wrap_command)

from .fakes import Clock, FakeClipboard, FakeRemote, FakeTarget


def make(**remote_kw):
    clip, clock = FakeClipboard(), Clock()
    remote = FakeRemote(clip, **remote_kw)
    bridge = ConsoleBridge(remote, FakeTarget(), clipboard=clip, sleep=clock.sleep, clock=clock)
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
    assert bridge.run("Get-Date").output == "previous command is still running"
    clip.text = f"{END}{bridge.pending} exit=0\nУспешно установлено"
    r = bridge.wait(60)
    assert r.status == "done" and "Успешно" in r.output and bridge.pending is None


def test_output_is_trimmed_for_model():
    bridge, _, _ = make(outputs={"big": ("x" * 50000, 0)})
    text = bridge.run("big").for_model(limit=1000)
    assert len(text) < 1200 and "[cut]" in text


def test_enduser_dialog_escapes_quotes():
    script = enduser_dialog("Can't you see 'it'?")
    assert "'Can''t you see ''it''?'" in script and "ANSWER: " in script
