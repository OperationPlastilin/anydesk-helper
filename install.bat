@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if not exist config.toml copy config.example.toml config.toml
echo.
echo Готово. Отредактируйте config.toml и запустите run.bat
pause
