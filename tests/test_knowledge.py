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
                   "Обновить App Installer из Store", ["store"])
    assert again["id"] == a["id"] and again["seen"] == 2 and set(again["tags"]) == {"winget", "store"}
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
                      "problem": "нужны Word и Excel", "solution": "ODT, O365BusinessRetail, ru-ru"}]},
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
