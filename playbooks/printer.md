keywords: принтер, печать, printer, print, сканер, scanner
- Список принтеров: `Get-Printer | select Name,PrinterStatus,PortName`.
- Очередь печати зависла: (PowerShell от администратора) `Stop-Service spooler -Force; Remove-Item C:\Windows\System32\spool\PRINTERS\* -Force; Start-Service spooler`.
- Принтер по умолчанию: `(Get-CimInstance Win32_Printer -Filter "Name='<имя>'") | Invoke-CimMethod -MethodName SetDefaultPrinter`.
- Пробная страница: `Invoke-CimMethod -InputObject (Get-CimInstance Win32_Printer -Filter "Name='<имя>'") -MethodName PrintTestPage`.
