@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python -m anydesk_helper %*
pause
