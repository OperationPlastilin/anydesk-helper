# CLAUDE.md — памятка по проекту (читать первой)

AnyDesk Helper: Windows-программа техника. Локальный веб-интерфейс → тикет → агент подключается
к ПК пользователя по AnyDesk и решает задачу, в первую очередь через PowerShell. Мышь и скриншоты
нужны только для окон вроде UAC. Модель — Qwen через OpenAI-совместимое облачное API. После
каждого тикета агент записывает уроки в базу опыта.

## Правила ведения (обязательно)
- Добавил или изменил функцию → **в том же коммите обнови этот файл**: mind map и нужный раздел.
  Пиши коротко, файл ≤ 150 строк. Это экономит токены после сжатия сессии.
- **README.md — короткий (≤ 80 строк), живой и интересный**: эмодзи, быстрый старт, фичи.
  Подробности пиши сюда или в docstring, не в README.
- Интерфейс, комментарии и сообщения на русском. Промты для модели на английском с
  «Write … in {language}».
- Любая новая логика → тест в `tests/` (подделки в `tests/fakes.py`, без реальных пауз: `Clock`).
- Фронтенд — чистый JS/CSS без сборки. Тяжёлых зависимостей не добавлять.

## Команды
```bash
python -m pytest -q                      # ~40 тестов; PowerShell-тесты идут только на Windows (CI)
python -m anydesk_helper --dev           # из исходников; --dev = режим «этот ПК» + dry-run в UI
python -m anydesk_helper --check         # проверка модели из настроек
./build.ps1                              # Windows: PyInstaller -> dist/AnyDeskHelper.exe + .sha256
git tag vX.Y.Z && git push origin vX.Y.Z # релиз: Actions собирает exe -> GitHub Release -> автообновление
```

## Mind map
```
AnyDeskHelper.exe (launcher.py -> anydesk_helper/__main__.py)
├─ startup: updater.check() -> новый релиз? скачать, сверить sha256, подменить exe .bat-скриптом, перезапуск
├─ web.py (Flask + waitress, 127.0.0.1:8765, токен X-Token / ?t=, проверка Host)
│  ├─ /api/tickets* (CRUD, start, answer, control pause|resume|stop, hint, events?after=, export .md)
│  ├─ /api/knowledge* (CRUD, export/import JSON)   ├─ /api/settings* (+models, +check)
│  └─ /api/state (версия, активный сеанс, counts, knowledge stats, model)   /api/update/*
├─ static/ (index.html, app.js, style.css) — SPA на hash-роутинге: #/ #/tickets #/new #/t/ID #/kb #/settings
│  └─ дизайн: тёмный docs-стиль (massgrave.dev/Docusaurus), admonitions .adm tip|info|warn|danger|note,
│     кодблоки .code с «копировать», навбар: вкладки + виджет агента + модель + обновление + тема
├─ session.py: SessionManager (один сеанс за раз) -> Session(thread) = «оператор» для агента
│  └─ default_factory: окно AnyDesk (anydesk.py) + Controller + ConsoleBridge + LLM
├─ agent.py: Agent.run() = prepare_console(проба) -> load_experience -> make_plan(вопросы, одобрение) -> loop
│  ├─ handle(action): run/wait/open_terminal/ask_enduser/screenshot/GUI/ask_operator/done/fail
│  ├─ known_fix(): команда упала -> поиск урока по тексту ошибки -> «KNOWN FROM EXPERIENCE»
│  └─ learn(outcome): рефлексия LLM -> 0–3 урока -> knowledge.add (дедупликация)
├─ bridge.py: PowerShell через буфер обмена AnyDesk (см. протокол ниже)
├─ prompts.py: PLAN_*, STEP_*, REFLECT_* + experience_block + load_playbooks(playbooks/*.md)
├─ actions.py: extract_json, parse_action, needs_confirmation (DANGEROUS_PATTERNS)
├─ knowledge.py: таблицы lessons + lesson_uses; tokens() (стем: первые 6 букв), search, scrub
├─ db.py: SQLite tickets + events (data JSON); при старте running/waiting -> stopped
├─ llm.py: chat() (json_mode с откатом, ретраи 429/5xx), list_models()
├─ controller.py (pyautogui, ленивый импорт), screen.py (mss, стороны кратны 28 px), checks.py, report.py
└─ config.py: Settings -> %LOCALAPPDATA%\AnyDeskHelper\settings.json; paths.py: data_dir/resource_dir
```

## Ключевые контракты
- **Оператор** (Session и FakeOperator): `event(kind, text, **data)`, `ask(kind, text, **data) -> str`
  (блокирует), `checkpoint() -> [подсказки]` (пауза, стоп → StopSession), `image(step, img) -> имя`.
  Ответы на ask: `yes` / `no` / `stop` / любой текст = подсказка или ответ.
  Виды ask: question, plan, confirm, done, fail, error, pick_window.
- **События** (UI рисует в `renderEvent`): status, info, warning, error, prompt (в панель «Промт»),
  plan, ask, answer, hint, note, step, command, experience, learned, done, failed.
- **Формат ответа модели** (шаг): один JSON, порядок ключей observation, plan_step, thought, action,
  параметры, risky. План: `{summary, questions[], plan[]}`. Рефлексия: `{lessons:[{kind,title,problem,solution,tags}]}`.
  Невалидный ответ → объясняем ошибку и переспрашиваем (до 3 раз).
- **Мост**: строка-обёртка (одна, ASCII) → base64-команда → `Write-Host 'AI> …'`, `Set-Clipboard 'AIH-RUN-<id>'`,
  dot-source ScriptBlock `*>&1 | Tee-Object` → `Set-Clipboard "AIH-END-<id> exit=N`n<вывод>"`.
  Нет START за 10 с → `no_ack` (консоль не в фокусе → скриншот). Переменные живут между командами.
- **GUI-клики** разрешены, только если в этом шаге был скриншот (`parse_action(img_w=0)` → ошибка).
- **Опыт**: search min_score 0.2, дедупликация по title+problem ≥ 0.5 того же kind; wins += 1, если
  тикет с использованным уроком решён; solution из нерешённого тикета → pitfall; scrub убирает email, ключи, пароли.

## Подводные камни
- В контейнере на Linux pyautogui без tkinter делает `sys.exit` при импорте (mouseinfo) — в тестах не
  импортировать, для ручных проверок подменять `sys.modules["mouseinfo"]`.
- Сеть контейнера: massgrave.dev и загрузка PowerShell заблокированы. PowerShell проверяется в CI (windows-latest).
- PyInstaller: `--add-data anydesk_helper/static;anydesk_helper/static` и `playbooks;playbooks`;
  web.py ищет static через resource_dir().
- В `ps … | grep` не пиши паттерн, совпадающий с собственной командной строкой (`kill` убьёт свою же оболочку).
