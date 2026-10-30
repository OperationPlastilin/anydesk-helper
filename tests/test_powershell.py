"""Проверка обёртки команд в настоящем PowerShell (запускается там, где он есть, — в CI на Windows)."""

import base64
import os
import shutil
import subprocess

import pytest

from anydesk_helper.bridge import INIT, PROBE, enduser_dialog, parse_clipboard, ps_text, wrap_command

PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PS, reason="PowerShell не установлен")

# Подменяем Set-Clipboard: пишем «буфер» в файл, чтобы тест не зависел от рабочего стола.
FAKE_CLIP = ("function Set-Clipboard { param([Parameter(Position=0)]$Value) "
             "[IO.File]::WriteAllText($env:CLIP_FILE, [string]$Value) }")


def run_session(tmp_path, commands, local_name=None):
    """Выполняет обёртки по очереди в ОДНОМ процессе PowerShell; возвращает разобранные результаты."""
    results = []
    script = [FAKE_CLIP]
    for i, cmd in enumerate(commands):
        clip = tmp_path / f"clip{i}.txt"
        script.append(f"$env:CLIP_FILE = '{clip}'")
        script.append(wrap_command(cmd, f"id{i}", local_name))
    ps1 = tmp_path / "session.ps1"
    ps1.write_text("\n".join(script), encoding="utf-8-sig")
    subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                   capture_output=True, timeout=120, env={**os.environ})
    for i in range(len(commands)):
        text = (tmp_path / f"clip{i}.txt").read_text(encoding="utf-8")
        results.append(parse_clipboard(text, f"id{i}"))
    return results


def test_wrapper_in_real_powershell(tmp_path):
    native = "cmd /c exit 3" if os.name == "nt" else "bash -c 'exit 3'"
    r = run_session(tmp_path, [
        "Get-Date -Year 2020 -Month 1 -Day 2 | ForEach-Object Year",
        "'Привет \"мир\"' + ' ' + 'it''s'  # комментарий",
        "$x = 21\n$x * 2",  # многострочная команда; переменная живёт между командами
        "$x + 1",
        "throw 'boom'",
        native,
        "if (",  # синтаксическая ошибка не должна ломать консоль
        f"{INIT}; 'ready'",
    ])
    assert all(x and x.status == "done" for x in r), r
    assert r[0].output.strip() == "2020"
    assert r[1].output.strip() == "Привет \"мир\" it's"
    assert r[2].output.strip() == "42"
    assert r[3].output.strip() == "22"
    assert "boom" in r[4].output
    assert r[5].exit_code == 3 and r[5].output.strip() == ""  # не вывод прошлых команд
    assert r[6].output.strip()  # текст ошибки разбора
    assert r[7].output.strip() == "ready"


def test_wrapper_fresh_output_and_preferences(tmp_path):
    stderr = ('cmd /c "echo err 1>&2 & echo out & exit 1"' if os.name == "nt"
              else "bash -c 'echo err >&2; echo out; exit 1'")
    r = run_session(tmp_path, [
        "'x'",
        "$y = 1",  # без вывода: раньше возвращался вывод прошлой команды (старый $__t)
        "'before'; throw 'boom2'",  # вывод до исключения не теряется
        "$ErrorActionPreference='Stop'; 'stop set'",  # не должно дожить до следующей команды
        stderr,  # в 5.1 stderr программы при Stop стал бы фатальной ошибкой
        "'a'; 1..3 | ForEach-Object { if ($_ -eq 2) { break }; $_ }",  # break вне цикла
        "'after'",
    ])
    assert all(x and x.status == "done" for x in r), r
    assert r[0].output.strip() == "x"
    assert r[1].output.strip() == ""
    assert "before" in r[2].output and "boom2" in r[2].output
    assert r[3].output.strip() == "stop set"
    assert r[4].exit_code == 1 and "err" in r[4].output and "out" in r[4].output
    assert r[5].output.split() == ["a", "1"]
    assert r[6].output.strip() == "after"


def test_enduser_dialog_in_real_powershell(tmp_path):
    question = "Нажмите ‘OK’; x’; $global:pwned=1; ’ Can't"
    b64 = base64.b64encode(enduser_dialog(question).encode()).decode()
    r = run_session(tmp_path, [
        # окно не показываем — только проверяем, что скрипт разбирается в этой версии PowerShell
        f"$null = [ScriptBlock]::Create([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{b64}'))); 'parsed'",
        ps_text(question),  # текст доходит до окна точно, с кавычками и кириллицей
    ])
    assert r[0].output.strip() == "parsed"
    assert r[1].output.strip() == question


@pytest.mark.skipif(os.name != "nt", reason="$env:COMPUTERNAME есть только в Windows")
def test_wrapper_refuses_on_technician_pc(tmp_path):
    marker = tmp_path / "ran.txt"
    cmd = f"Set-Content -LiteralPath '{marker}' -Value 1; 'ran'"
    (r,) = run_session(tmp_path, [cmd], local_name=os.environ["COMPUTERNAME"].lower())
    assert r.status == "refused" and r.exit_code == -999 and not marker.exists()
    r = run_session(tmp_path, [cmd, "$z = 5", "$z * 2"], local_name="NOT-" + os.environ["COMPUTERNAME"])
    assert all(x.status == "done" for x in r) and r[0].output.strip() == "ran" and marker.exists()
    assert r[2].output.strip() == "10"  # и внутри if/else переменные живут между командами


@pytest.mark.skipif(os.name != "nt", reason="cmd /u есть только в Windows")
def test_utf16_output_has_no_nul(tmp_path):
    # sfc и др. пишут UTF-16: буфер обмена обрезал бы текст на первом NUL
    (r,) = run_session(tmp_path, ["cmd /u /c echo Windows"])
    assert r.status == "done" and "\x00" not in r.output
    assert "Windows" in r.output and "UTF-16" in r.output


@pytest.mark.skipif(os.name != "nt", reason="проба системы рассчитана на Windows")
def test_probe(tmp_path):
    (r,) = run_session(tmp_path, [PROBE])
    assert r.status == "done" and "PC:" in r.output and "admin:" in r.output
