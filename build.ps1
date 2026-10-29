# Сборка AnyDeskHelper.exe (один файл, все зависимости внутри). Запускается в GitHub Actions,
# можно и локально на Windows: pip install -r requirements.txt pyinstaller; ./build.ps1
$ErrorActionPreference = "Stop"
python -m PyInstaller --noconfirm --clean --onefile --console `
    --name AnyDeskHelper `
    --icon assets/icon.ico `
    --add-data "anydesk_helper/static;anydesk_helper/static" `
    --add-data "playbooks;playbooks" `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
$hash = (Get-FileHash dist/AnyDeskHelper.exe -Algorithm SHA256).Hash.ToLower()
Set-Content -Path dist/AnyDeskHelper.exe.sha256 -Value "$hash  AnyDeskHelper.exe" -NoNewline -Encoding ascii
Write-Host "Готово: dist/AnyDeskHelper.exe ($hash)"
