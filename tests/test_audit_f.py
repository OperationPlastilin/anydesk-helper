"""Методички playbooks/*.md: формат, выбор по тексту тикета, совместимость с русской Windows."""

import re
from pathlib import Path

import pytest

from anydesk_helper.prompts import load_playbooks, plan_system, step_system

PLAYBOOKS = Path(__file__).resolve().parent.parent / "playbooks"
FILES = sorted(PLAYBOOKS.glob("*.md"))


def selected(task: str) -> set[str]:
    return set(re.findall(r"^## (\S+)$", load_playbooks([PLAYBOOKS], task), re.M))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_playbook_format(path):
    text = path.read_bytes().decode("utf-8")  # UTF-8 без BOM
    first, _, body = text.partition("\n")
    assert first.startswith("keywords:")
    keywords = [k.strip() for k in first.split(":", 1)[1].split(",")]
    assert all(keywords) and all(k == k.lower() for k in keywords)  # текст тикета приводится к нижнему регистру
    assert len(body.strip().splitlines()) <= 20  # методички едут в каждый промт шага


def test_bundled_playbooks_are_new_ones_too():
    assert {"general", "office", "printer", "network", "update", "cleanup", "1c", "crypto"} <= {
        p.stem for p in FILES}


@pytest.mark.parametrize("task, expected, unexpected", [
    ("Очистить кэш 1С", {"1c"}, {"cleanup", "crypto"}),
    ("Не запускается 1c бухгалтерия", {"1c"}, set()),
    ("Не работает ЭЦП", {"crypto"}, set()),
    ("не работает подпись, КриптоПро", {"crypto"}, set()),
    ("Не виден Рутокен, сертификат истёк", {"crypto"}, set()),
    ("Настроить подпись в Outlook", {"office"}, {"crypto"}),
    ("мало места на диске", {"cleanup"}, set()),
    ("Тормозит компьютер", {"cleanup"}, set()),
    ("нет сетевого подключения", {"network"}, set()),
    ("Нет интернета, сайты не открываются", {"network"}, set()),
    ("Не ставятся обновления Windows, ошибка 0x80240017", {"update"}, set()),
    ("Проверить системные файлы (sfc)", {"update"}, set()),
    ("Установить Office", {"office"}, {"network", "update", "cleanup", "1c", "crypto"}),
    ("Установить Office на диск D", {"office"}, {"cleanup"}),
    ("update office", {"office"}, {"update"}),
    ("Принтер Эпсон не печатает", {"printer"}, {"crypto", "network"}),
    ("Сбросить пароль пользователя", set(), {"office", "printer", "network", "update", "cleanup",
                                             "1c", "crypto"}),
])
def test_ticket_selects_expected_playbooks(task, expected, unexpected):
    got = selected(task)
    assert "general" in got
    assert expected <= got, got
    assert not unexpected & got, got


def test_admin_check_does_not_depend_on_localized_group_names():
    """На русской Windows группа называется «Администраторы»: IsInRole('Administrators') = False."""
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert "IsInRole('" not in text and 'IsInRole("' not in text, path.name
        assert not re.search(r"net localgroup ['\"]?Administrators", text, re.I), path.name
        assert not re.search(r"-Group ['\"]?Administrators", text, re.I), path.name
    general = (PLAYBOOKS / "general.md").read_text(encoding="utf-8")
    assert "IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)" in general
    assert "S-1-5-32-544" in general


def test_downloads_enable_tls12_for_powershell_51():
    for path in FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "Invoke-WebRequest" in line:
                assert "SecurityProtocol='Tls12'" in line, path.name


def test_all_playbooks_fit_into_prompts():
    """Фигурные скобки в командах ({8}, @{n=..}) не ломают str.format промтов."""
    everything = load_playbooks([PLAYBOOKS], " ".join(
        "office принтер интернет обновления тормозит 1с эцп".split()))
    assert len(re.findall(r"^## ", everything, re.M)) == len(FILES)
    for system in (step_system("Russian", True, "pixels", everything),
                   plan_system("Russian", everything)):
        assert "[0-9a-f]{8}" in system and "@{n='Name'" in system


def test_playbooks_do_not_push_minute_by_minute_polling():
    """Каждый ответ модели тратит запрос из маленькой дневной квоты: wait — от 300 с (как в STEP_SYSTEM)."""
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        for seconds in re.findall(r"\bwait (?:по )?(\d+)", text):
            assert int(seconds) >= 300, (path.name, seconds)
    office = (PLAYBOOKS / "office.md").read_text(encoding="utf-8")
    assert "STILL RUNNING" in office and "wait 600" in office


def test_sfc_recipe_decodes_utf16_and_is_always_loaded():
    """sfc пишет UTF-16: без OutputEncoding=Unicode модель получает «W\\0i\\0n…» или обрывок."""
    text = load_playbooks([PLAYBOOKS], "Тормозит компьютер, битые файлы")
    assert "sfc /scannow" in text
    assert "[Console]::OutputEncoding=[Text.Encoding]::Unicode" in text
    # кодировка возвращается в finally: команды dot-source'ятся, иначе она жила бы дальше
    assert "finally { [Console]::OutputEncoding=$e }" in text
    assert r"CBS.log -Pattern '\[SR\]'" in text
    for path in FILES:  # нигде нет противоречащего совета «вывод sfc выбросить»
        assert "sfc /scannow | Out-Null" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_code_spans_are_closed(path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert line.count("`") % 2 == 0, (path.name, n)
