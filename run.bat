@echo off
chcp 65001 >nul
rem Запуск из исходников (для разработки). Обычным пользователям нужен AnyDeskHelper.exe из Releases.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m anydesk_helper %*
pause
