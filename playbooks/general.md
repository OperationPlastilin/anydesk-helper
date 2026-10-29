keywords: *
- Консоль: open_terminal (powershell). Для установки ПО и смены настроек нужен admin=true; после этого появится окно UAC — нажать «Да» (если экран чёрный — ask_user).
- Установка программ: `winget install --id <Id> -e --silent --accept-package-agreements --accept-source-agreements`. Найти Id: `winget search <название>`.
- Перед скачиванием файлов: `$ProgressPreference='SilentlyContinue'` — иначе Invoke-WebRequest очень медленный.
- Проверка интернета: `Test-NetConnection 8.8.8.8 -Port 53 | select TcpTestSucceeded`.
- Место на диске: `Get-PSDrive C | select Used,Free`.
- Версия Windows: `(Get-CimInstance Win32_OperatingSystem).Caption`.
- Если команда длинная — сначала дождаться, пока снова появится приглашение «PS C:\...>», только потом вводить следующую.
