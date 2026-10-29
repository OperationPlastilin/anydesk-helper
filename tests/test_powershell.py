"""Проверка обёртки команд в настоящем PowerShell (запускается там, где он есть, — в CI на Windows)."""

import os
import shutil
import subprocess

import pytest

from anydesk_helper.bridge import INIT, PROBE, parse_clipboard, wrap_command

PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PS, reason="PowerShell не установлен")

# Подменяем Set-Clipboard: пишем «буфер» в файл, чтобы тест не зависел от рабочего стола.
FAKE_CLIP = ("function Set-Clipboard { param([Parameter(Position=0)]$Value) "
             "[IO.File]::WriteAllText($env:CLIP_FILE, [string]$Value) }")


def run_session(tmp_path, commands):
    """Выполняет обёртки по очереди в ОДНОМ процессе PowerShell; возвращает разобранные результаты."""
    results = []
    script = [FAKE_CLIP]
    for i, cmd in enumerate(commands):
        clip = tmp_path / f"clip{i}.txt"
        script.append(f"$env:CLIP_FILE = '{clip}'")
        script.append(wrap_command(cmd, f"id{i}"))
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
    assert r[5].exit_code == 3
    assert r[6].output.strip()  # текст ошибки разбора
    assert r[7].output.strip() == "ready"


@pytest.mark.skipif(os.name != "nt", reason="проба системы рассчитана на Windows")
def test_probe(tmp_path):
    (r,) = run_session(tmp_path, [PROBE])
    assert r.status == "done" and "PC:" in r.output and "admin:" in r.output
