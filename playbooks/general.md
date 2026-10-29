keywords: *
- Права администратора: проверь `([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')`. Если False, а нужна установка или правка системы — open_terminal с admin=true и подтверди UAC.
- Установка программ: `winget install --id <Id> -e --silent --accept-package-agreements --accept-source-agreements`. Найти Id: `winget search <название> | Select-Object -First 15`.
- Если winget нет: `Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe`.
- Скачивание файла: `Invoke-WebRequest <url> -OutFile <путь> -UseBasicParsing`.
- Проверка интернета: `Test-NetConnection 8.8.8.8 -Port 53 | Select-Object TcpTestSucceeded`.
- Место на диске: `Get-PSDrive C | Select-Object Used, Free`.
- Установленные программы: `Get-Package | Where-Object Name -like '*<имя>*' | Select-Object Name, Version`.
- Процессы, которые грузят ПК: `Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, CPU, WorkingSet`.
