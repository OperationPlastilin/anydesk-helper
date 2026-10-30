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
python -m pytest -q                      # ~300 тестов; PowerShell-тесты идут только на Windows (CI)
python -m anydesk_helper --dev           # из исходников; --dev = режим «этот ПК» + dry-run в UI
python -m anydesk_helper --check         # проверка модели из настроек
./build.ps1                              # Windows: PyInstaller -> dist/AnyDeskHelper.exe + .sha256
git tag vX.Y.Z && git push origin vX.Y.Z # релиз: Actions собирает exe -> GitHub Release -> автообновление
```

## Mind map
```
AnyDeskHelper.exe (launcher.py -> anydesk_helper/__main__.py)
├─ startup: updater.check() -> новый релиз? скачать, сверить sha256, подменить exe .bat-скриптом, перезапуск
├─ web.py (Flask + waitress, 127.0.0.1:8765; токен постоянный в data_dir/token, нужен и для «/» (?t=), и для
│  API (X-Token); проверка Host; X-Frame-Options DENY)
│  ├─ /api/tickets* (CRUD, start, answer, control pause|resume|stop, hint, events?after=, export .md)
│  ├─ /api/knowledge* (CRUD, export/import JSON)   ├─ /api/settings* (+models, +check)
│  └─ /api/state (версия, активный сеанс, counts, knowledge stats, model)   /api/update/*
├─ static/ (index.html, app.js, style.css) — SPA на hash-роутинге: #/ #/tickets #/new #/t/ID #/kb #/settings
│  └─ дизайн: тёмный docs-стиль (massgrave.dev/Docusaurus), admonitions .adm tip|info|warn|danger|note,
│     кодблоки .code с «копировать», навбар: вкладки + виджет агента + модель + обновление + тема
├─ session.py: SessionManager (один сеанс за раз; при старте cleanup_files(keep_files_days)) -> Session(thread)
│  ├─ Session = «оператор» для агента; resume=True -> restore_state(events) -> Agent.run(saved)
│  └─ default_factory: окно AnyDesk + Controller(target) + ConsoleBridge(local_name=COMPUTERNAME техника)
│     + LLM(should_stop, notify); после Стопа/аварийной остановки рефлексии нет
├─ agent.py: Agent.run(resume) = prepare_console(проба) -> load_experience -> make_plan | resume -> loop
│  ├─ StopSession во время команды -> cancel_command() = Ctrl+C в консоли пользователя
│  ├─ remote(): FocusError -> ask("error") повторить/стоп; FailSafe -> StopSession; status refused -> StopSession
│  ├─ после ответа модели снова checkpoint() (Стоп во время запроса не выполняет действие); wait дробится
│     на части с проверкой Стопа/подсказок; обрезанный ответ (finish_reason=length) -> переспрос
│  ├─ handle(action): run/wait/open_terminal/ask_enduser/screenshot/GUI/ask_operator/done/fail
│  ├─ known_fix(): команда упала -> поиск урока по тексту ошибки -> «KNOWN FROM EXPERIENCE»
│  └─ learn(outcome): рефлексия LLM -> 0–3 урока -> knowledge.add; урок с удалённым кодом/из ответов
│     пользователя -> выключен «на проверке»
├─ bridge.py: PowerShell через буфер обмена AnyDesk (см. протокол ниже)
├─ prompts.py: PLAN_*, STEP_*, REFLECT_* + experience_block + load_playbooks(playbooks/*.md)
├─ actions.py: extract_json (берёт ПОСЛЕДНИЙ JSON вне <think>), parse_action (exit запрещён, вложенные
│  параметры Qwen), needs_confirmation (DANGEROUS_PATTERNS + is_remote_code: iex, -enc, скачать-и-запустить)
├─ knowledge.py: таблицы lessons + lesson_uses; tokens() (стем: первые 6 букв), search, scrub
├─ db.py: SQLite tickets + events (data JSON); при старте running/waiting -> stopped
├─ llm.py: chat() (json_mode с откатом, ретраи 429/5xx c Retry-After, дневной лимит -> fallback_model,
│  пустой/обрезанный ответ -> поднять max_tokens), finish_reason, list_models()
├─ controller.py (pyautogui, ленивый импорт; focus() перед КАЖДЫМ вводом; латиница по VK-кодам — не зависит от
│  раскладки; Esc перед повтором недопечатанной строки), anydesk.py (WindowTarget.focus проверяет
│  GetForegroundWindow, иначе FocusError; переподключение окна), screen.py, checks.py, report.py
├─ playbooks/: general (всегда), office, printer, network, update (+sfc/DISM), cleanup, 1c, crypto (КриптоПро/ЭЦП)
└─ config.py: Settings -> %LOCALAPPDATA%\AnyDeskHelper\settings.json (api_key = "dpapi:<b64>", DPAPI
   текущего пользователя; не расшифровался -> пусто); paths.py: data_dir/resource_dir
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
- **Мост**: строка-обёртка (одна, ASCII): если `$env:COMPUTERNAME` = ПК техника → `exit=-999 REFUSED` (status
  refused). Иначе сброс `$ErrorActionPreference='Continue'` и т.п. → base64-команда → `Write-Host 'AI> …'` →
  `Set-Clipboard 'AIH-RUN-<id>'` → `do { . ScriptBlock *>&1 | ForEach-Object {$__aih.Add($_); $_} } while($false)`
  (свой список на команду, не Tee-Object) → убрать NUL → `Set-Clipboard "AIH-END-<id> exit=N`n<вывод>"`.
  После результата в буфер кладётся `# AIH idle`. Нет START за max(timeout,10) с → `no_ack`. Пока команда
  идёт, новая не отправляется (status busy). Переменные живут между командами. ask_enduser: текст в base64,
  диалог с таймером 240 с, ответ всегда `ANSWER: …`.
- **Таймаут команды**: `Action.timeout=None` → `settings.command_timeout`; модель может задать свой (5–1800).
- **Продолжение**: restore_state берёт из событий план, Q/A, подсказки и историю шагов, план заново не строится.
- **GUI-клики** разрешены, только если в этом шаге был скриншот (`parse_action(img_w=0)` → ошибка).
- **Опыт**: search min_score 0.2; дедупликация того же kind, только среди включённых и не ручных, решение не
  затирается вслепую; разные коды ошибок (0x…, KB…) не сливаются; wins считается один раз на тикет;
  solution из нерешённого тикета → pitfall; scrub убирает email, ключи, пароли (net use, -Password, ConvertTo-SecureString…).

## Подводные камни
- В контейнере на Linux pyautogui без tkinter делает `sys.exit` при импорте (mouseinfo) — в тестах не
  импортировать, для ручных проверок подменять `sys.modules["mouseinfo"]`.
- Сеть контейнера: massgrave.dev и загрузка PowerShell заблокированы. PowerShell проверяется в CI (windows-latest).
- PyInstaller: `--add-data anydesk_helper/static;anydesk_helper/static` и `playbooks;playbooks`;
  web.py ищет static через resource_dir().
- Релиз: smoke-тест в release.yml берёт токен из `%LOCALAPPDATA%\AnyDeskHelper\token` (без него «/» = 401).
- Самообновление onefile: перезапуск с PYINSTALLER_RESET_ENVIRONMENT=1 (нужен PyInstaller ≥ 6.9), .bat через
  CREATE_NO_WINDOW (скрытая консоль, а не DETACHED_PROCESS).
- Тесты с Session.ask: отвечать, когда `pending` опубликован (`answer_when_asked`), а не по таймеру — на Windows
  запись в SQLite медленнее; в ask сначала статус и событие, потом pending; звук — в отдельном потоке.
- Проверка сборки без релиза: запустить release.yml вручную (workflow_dispatch) — публикуется только артефакт.
- В `ps … | grep` не пиши паттерн, совпадающий с собственной командной строкой (`kill` убьёт свою же оболочку).
