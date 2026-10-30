from anydesk_helper.agent import YES
from anydesk_helper.db import DB
from anydesk_helper.knowledge import KnowledgeBase, looks_like_error, prompt_block, scrub, tokens

from .test_agent import make_agent


def kb_with(tmp_path):
    return KnowledgeBase(DB(tmp_path / "t.db"))


def test_tokens_stem_russian_words():
    assert "принте" in tokens("Не печатает принтера HP") and "принте" in tokens("принтер")
    assert not tokens("для что как")


def test_scrub_personal_data():
    s = scrub("Логин ivanova@corp.ru, ключ ABCDE-12345-FGHIJ-67890-KLMNO, -Password 'qwerty'")
    assert "corp.ru" not in s and "ABCDE" not in s and "qwerty" not in s


def test_search_dedup_and_ranking(tmp_path):
    kb = kb_with(tmp_path)
    a = kb.add("error_fix", "winget не найден на Windows 10",
               "winget : The term 'winget' is not recognized", "Add-AppxPackage -RegisterByFamilyName ...",
               ["winget"], source_ticket=1)
    kb.add("solution", "Очистка зависшей очереди печати", "Документы висят в очереди принтера",
           "Stop-Service spooler -Force; ...", "printer, принтер")
    again = kb.add("error_fix", "winget не найден на Windows 10", "The term 'winget' is not recognized",
                   "Add-AppxPackage -RegisterByFamilyName, затем обновить App Installer из Store", ["store"])
    assert again["id"] == a["id"] and again["seen"] == 2 and set(again["tags"]) == {"winget", "store"}
    assert again["solution"] == a["solution"]  # сохранённый рецепт не затирается
    assert len(kb.list()) == 2

    hits = kb.search("Принтер не печатает, документы в очереди")
    assert hits and hits[0]["title"].startswith("Очистка")
    assert kb.search("winget is not recognized as the name of a cmdlet")[0]["id"] == a["id"]
    assert kb.search("совершенно другая тема про бухгалтерию") == []

    kb.update(a["id"], enabled=False)
    assert not kb.search("winget is not recognized")


def test_uses_and_wins(tmp_path):
    kb = kb_with(tmp_path)
    x = kb.add("solution", "Установка Office через ODT", "office", "setup.exe /configure")
    kb.mark_used(5, [x["id"]])
    kb.mark_used(5, [x["id"]])  # повтор в том же тикете не считается
    kb.mark_outcome(5, solved=True)
    x = kb.get(x["id"])
    assert (x["uses"], x["wins"]) == (1, 1)
    assert "helped in 1 of 1" in prompt_block([x])


def test_export_import(tmp_path):
    kb = kb_with(tmp_path)
    kb.add("manual", "Совет", "когда", "что делать")
    other = KnowledgeBase(DB(tmp_path / "other.db"))
    import json
    assert other.import_(json.loads(kb.export())) == 1 and other.list()[0]["title"] == "Совет"


def test_looks_like_error():
    assert looks_like_error("", 1)
    assert looks_like_error("winget : The term 'winget' is not recognized", 0)
    assert not looks_like_error("Successfully installed", 0)


def test_agent_uses_experience_and_learns(tmp_path):
    kb = kb_with(tmp_path)
    kb.add("solution", "Установка Microsoft Office через ODT", "Установить Office, Word, Excel",
           "Скачать setup.exe с officecdn, config.xml, setup.exe /configure", ["office"])
    fix = kb.add("error_fix", "winget не найден", "winget is not recognized",
                 "Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe",
                 ["winget"])
    replies = [
        {"summary": "", "questions": [], "plan": ["Поставить Office"]},
        {"action": "run", "command": "winget install Microsoft.Office", "risky": False},
        {"action": "done", "summary": "ok"},
        {"lessons": [{"kind": "error_fix", "title": "winget отсутствует на чистой Windows 10",
                      "problem": "winget is not recognized", "solution": "Add-AppxPackage ... затем winget",
                      "tags": ["winget"]},
                     {"kind": "solution", "title": "Office для бухгалтерии ставится через ODT за 15 минут",
                      "problem": "нужны Word и Excel",
                      "solution": "ODT: setup.exe /configure, O365BusinessRetail, ru-ru"}]},
    ]
    outputs = {"COMPUTERNAME": "PC: X", "winget": ("winget : The term 'winget' is not recognized", 1)}
    agent, llm, op, _ = make_agent(replies, [YES, YES], outputs, confirm="none")
    agent.kb = kb
    assert agent.run() is True
    # опыт попал в промт плана, а подсказка по ошибке — в результат команды
    assert "officecdn" in llm.requests[0][0]["content"]
    assert "KNOWN FROM EXPERIENCE" in llm.requests[2][1]["content"] and "DesktopAppInstaller" in llm.requests[2][1]["content"]
    assert kb.get(fix["id"])["uses"] == 1

    saved = agent.learn("solved")
    assert len(saved) == 2 and "learned" in op.kinds()
    assert "winget is not recognized" in llm.requests[-1][1]["content"]  # ошибки ушли в рефлексию
    # оба урока — перефразировки уже известных: база не разрастается, уроки «усиливаются»
    assert len(kb.list()) == 2 and all(x["seen"] == 2 for x in kb.list())


# ------------------------------------------------------------ находки аудита (#58, #59, #64, #65, #67)

def test_scrub_hides_powershell_and_net_passwords():
    leaks = {
        "Set-LocalUser -Name ivan -Password (ConvertTo-SecureString 'Qwerty123!' -AsPlainText -Force)": "Qwerty123!",
        "$s = ConvertTo-SecureString -AsPlainText -Force -String \"Secr3t\"": "Secr3t",
        "$pw = 'Pa55'; Set-LocalUser ivan -Password (ConvertTo-SecureString $pw -AsPlainText -Force)": "Pa55",
        "-Password 'my secret pass'": "secret",
        "net user ivan Qwerty123 /add": "Qwerty123",
        'net user "Ivan Petrov" "my pass" /add': "my pass",
        "cmdkey /generic:srv /user:ivan /pass:Qwerty123": "Qwerty123",
        r"net use Z: \\srv\share Qwerty123 /user:corp\ivan": "Qwerty123",
        "пароль: Qwerty": "Qwerty",
        # /user: перед паролем, конвейер в ConvertTo-SecureString, [adsi], пароль автовхода
        r"net use Z: \\srv\share /user:corp\ivan Qwerty123": "Qwerty123",
        r"net use \\srv\share /user:corp\ivan Qwerty123 /persistent:yes": "Qwerty123",
        "'Qwerty123' | ConvertTo-SecureString -AsPlainText -Force": "Qwerty123",
        "([adsi]'WinNT://./ivan,user').SetPassword('Qwerty123')": "Qwerty123",
        "([adsi]'WinNT://./ivan,user').ChangePassword('Old1pass','Qwerty123')": "Old1pass",
        '$u.Invoke("SetPassword", "Qwerty123")': "Qwerty123",
        r"reg add HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon /v DefaultPassword /t REG_SZ "
        r"/d Qwerty123 /f": "Qwerty123",
        r"Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -Name DefaultPassword "
        r"-Value 'Qwerty123'": "Qwerty123",
        '"DefaultPassword"="Qwerty123"': "Qwerty123",
    }
    for text, secret in leaks.items():
        clean = scrub(text)
        assert secret not in clean and "<скрыто>" in clean, (text, clean)
        assert scrub(clean) == clean  # повторный scrub (update после add) ничего не портит
    assert "ConvertTo-SecureString '<скрыто>' -AsPlainText" in scrub(next(iter(leaks)))  # рецепт цел


def test_scrub_keeps_harmless_text():
    for text in ("Set-LocalUser ivan -PasswordNeverExpires $true",
                 "Сменить пароль пользователя через net user",
                 "Убедитесь что пароль не истёк",
                 "net user ivan /active:yes", "net user ivan *", "net user ivan | findstr Password",
                 "New-LocalUser ivan -Password (Read-Host -AsSecureString)",
                 "Set-LocalUser ivan -Password $pw",
                 # русское слово после имени — не пароль; ключ без значения не съедает следующую строку
                 "Проверить через net user ivan что учётка активна",
                 "Set-LocalUser ivan -Password\nRestart-Computer", "пароль:\nСменить при входе",
                 "cmdkey /generic:srv /pass:\nnext", r"net use Z: \\srv\share /user:corp\ivan *",
                 "$u.SetPassword($pw)", "Set-ItemProperty $wl -Name DefaultPassword -Value $pw; $x = 'abc'"):
        assert scrub(text) == text, text
    # $password = 'X' и $pw = 'X' скрываются одинаково — кавычки (рабочий синтаксис) на месте
    assert scrub("$password = 'X1'") == "$password = '<скрыто>'"
    assert scrub("$pw = 'X1'") == "$pw = '<скрыто>'"


def test_tokens_codes_short_terms_and_yo():
    assert tokens("0x80070005 KB5034441") == {"0x80070005", "kb5034441"}
    assert tokens("0x80070005") != tokens("0x80070002")
    assert tokens("Не запускается 1С") == tokens("не запускается 1C") == {"1с", "запуск"}
    assert {"ip", "ad"} <= tokens("Нет IP в AD")
    assert not tokens("не на по всё")  # частицы и «всё» (= «все» из STOP) — не слова
    assert tokens("учётная") == tokens("учетная")


def test_search_ignores_lessons_about_other_error_code(tmp_path):
    kb = kb_with(tmp_path)
    denied = kb.add("error_fix", "Ошибка установки обновления 0x80070005", "Отказано в доступе при обновлении",
                    "Сбросить компоненты Windows Update от администратора")
    query = "Install-WindowsUpdate : Ошибка установки обновления 0x80070002"
    assert kb.search(query) == []
    assert kb.search("Ошибка установки обновления 0x80070005")[0]["id"] == denied["id"]
    # и при обучении это разные уроки
    other = kb.add("error_fix", "Ошибка установки обновления 0x80070002", "Отказано в доступе при обновлении",
                   "Сбросить компоненты Windows Update и очистить SoftwareDistribution")
    assert other["id"] != denied["id"] and len(kb.list()) == 2
    # код ошибки и номер обновления — разные вещи: урок про KB находится по коду ошибки
    winre = kb.add("error_fix", "KB5034441 не устанавливается", "мал раздел WinRE",
                   "Расширить раздел восстановления WinRE")
    assert [x["id"] for x in kb.list(query="Ошибка 0x80070643, раздел WinRE")] == [winre["id"]]


def test_hex_value_in_solution_is_not_an_error_code(tmp_path):
    """Коды сверяются по заголовку, проблеме и тегам: 0x00000000 в решении — значение реестра."""
    kb = kb_with(tmp_path)
    printer = kb.add("error_fix", "Не подключается общий принтер", "Windows не подключает общий принтер, ошибка печати",
                     r"reg add HKLM\System\CurrentControlSet\Control\Print /v RpcAuthnLevelPrivacyEnabled "
                     "/t REG_DWORD /d 0x00000000 /f; Restart-Service Spooler")
    for query in ("Не подключается сетевой принтер, ошибка 0x0000011b",
                  "Не подключается сетевой принтер, ошибка 0x00000709", "Не подключается сетевой принтер"):
        assert [x["id"] for x in kb.search(query)] == [printer["id"]], query


def test_search_finds_1c_lesson_not_outlook(tmp_path):
    kb = kb_with(tmp_path)
    kb.add("solution", "Не запускается Outlook", "Outlook закрывается при запуске", "outlook.exe /safe")
    one_c = kb.add("solution", "Не запускается 1С", "1С:Предприятие не открывается", "Очистить кэш 1С")
    hits = kb.search("Не запускается 1C")
    assert hits[0]["id"] == one_c["id"] and all(h["score"] < hits[0]["score"] for h in hits[1:])
    assert kb.list(query="1С")[0]["id"] == one_c["id"]


def test_dedup_keeps_proven_solution_and_separates_other_fix(tmp_path):
    kb = kb_with(tmp_path)
    spool = kb.add("error_fix", "Принтер не печатает", "Документы висят в очереди, принтер не печатает",
                   "Stop-Service Spooler; Remove-Item C:\\Windows\\System32\\spool\\PRINTERS\\*; Start-Service Spooler")
    kb.mark_used(1, [spool["id"]])
    kb.mark_outcome(1, solved=True)
    # та же проблема, тот же рецепт другими словами — усиливаем, решение не трогаем
    same = kb.add("error_fix", "Принтер не печатает", "Принтер не печатает, документы в очереди",
                  "Перезапустить Spooler и очистить папку PRINTERS")
    assert same["id"] == spool["id"] and same["seen"] == 2
    assert same["solution"] == spool["solution"] and same["wins"] == 1
    # тот же симптом, но другая причина и совсем другое решение — отдельный урок
    driver = kb.add("error_fix", "Принтер не печатает", "Принтер HP не печатает, документы в очереди",
                    "Скачать и установить HP Universal Print Driver")
    assert driver["id"] != spool["id"] and len(kb.list()) == 2
    assert kb.get(spool["id"])["solution"].startswith("Stop-Service Spooler")


def test_dedup_picks_best_match_and_fills_empty_solution(tmp_path):
    kb = kb_with(tmp_path)
    exact = kb.add("solution", "Очистка очереди печати принтера", "Документы висят в очереди печати")
    kb.add("solution", "Очистка очереди печати", "Документы висят")  # новее, но менее похож
    merged = kb.add("solution", "Очистка очереди печати принтера", "Документы висят в очереди печати",
                    "Restart-Service Spooler")
    assert merged["id"] == exact["id"] and merged["solution"] == "Restart-Service Spooler"


def test_dedup_disabled_lesson_stays_disabled(tmp_path):
    kb = kb_with(tmp_path)
    bad = kb.add("solution", "Нет интернета", "Не открываются сайты", "netsh winsock reset и перезагрузка")
    kb.update(bad["id"], enabled=False)
    again = kb.add("solution", "Нет интернета", "Не открываются сайты", "netsh winsock reset")
    assert again["id"] == bad["id"] and again["enabled"] is False  # отвергнутое не включается снова
    assert again["solution"] == "netsh winsock reset и перезагрузка"
    dns = kb.add("solution", "Нет интернета", "Не открываются сайты", "Set-DnsClientServerAddress 8.8.8.8")
    assert dns["id"] != bad["id"] and dns["enabled"] is True


def test_manual_lesson_without_dedup_is_always_saved(tmp_path):
    kb = kb_with(tmp_path)
    a = kb.add("manual", "Принтер HP не печатает", "", "Проверить кабель", dedup=False)
    b = kb.add("manual", "Принтер HP не печатает — нужен драйвер", "", "Проверить кабель", dedup=False)
    assert a["id"] != b["id"] and len(kb.list()) == 2


def test_wins_counted_once_per_ticket(tmp_path):
    kb = kb_with(tmp_path)
    x = kb.add("solution", "Установка Office через ODT", "office", "setup.exe /configure")
    for _ in range(3):  # «Запустить заново» решённого тикета
        kb.mark_used(5, [x["id"]])
        kb.mark_outcome(5, solved=True)
    kb.mark_used(6, [x["id"]])
    kb.mark_outcome(6, solved=True)
    x = kb.get(x["id"])
    assert (x["uses"], x["wins"]) == (2, 2)


def test_wins_migration_keeps_old_solved_tickets_credited(tmp_path):
    db = DB(tmp_path / "t.db")
    kb = KnowledgeBase(db)
    x = kb.add("solution", "Установка Office через ODT", "office", "setup.exe /configure")
    tid = db.create_ticket("Office")["id"]
    kb.mark_used(tid, [x["id"]])
    kb.mark_outcome(tid, solved=True)
    db.update_ticket(tid, status="solved")
    with db.lock, db.conn:  # база старой версии: таблицы lesson_wins ещё нет
        db.conn.execute("DROP TABLE lesson_wins")
    kb = KnowledgeBase(db)
    kb.mark_outcome(tid, solved=True)
    assert kb.get(x["id"])["wins"] == 1


def test_export_import_keeps_disabled_and_counts_only_new(tmp_path):
    import json
    kb = kb_with(tmp_path)
    kb.add("pitfall", "Не удалять папку System32", "чистка диска", "Remove-Item C:\\Windows\\System32")
    bad = kb.list()[0]
    kb.update(bad["id"], enabled=False)
    kb.add("solution", "Очистка диска", "мало места на C:", "cleanmgr /sagerun:1")
    dump = json.loads(kb.export())
    assert {x["title"]: x["enabled"] for x in dump}["Не удалять папку System32"] is False

    other = KnowledgeBase(DB(tmp_path / "other.db"))
    assert other.import_(dump) == 2
    assert {x["title"]: x["enabled"] for x in other.list()} == {"Не удалять папку System32": False,
                                                                "Очистка диска": True}
    # восстановление из своей же копии: ничего не добавлено и не «усилено»
    assert kb.import_(dump) == 0 and all(x["seen"] == 1 for x in kb.list())
    # похожий урок сливается, но не считается новым
    similar = [{"kind": "solution", "title": "Очистка диска", "problem": "мало места на диске C:",
                "solution": "cleanmgr /sagerun:1 и очистка временных файлов"}]
    assert other.import_(similar) == 0 and len(other.list()) == 2


def test_import_skips_broken_items(tmp_path):
    kb = kb_with(tmp_path)
    items = [{"kind": "manual", "title": 123}, {"title": "x", "tags": 5}, "строка", {"kind": "bogus", "title": "y"},
             {"title": None}, {"title": "Нормальный урок", "tags": ["a", 1]}]
    assert kb.import_(items) == 3
    assert {x["title"] for x in kb.list()} == {"123", "x", "Нормальный урок"}
