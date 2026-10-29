keywords: office, офис, word, excel, ворд, эксель, outlook, powerpoint, 365
Установка Microsoft Office.
1. Сначала спросить оператора (если не сказано в задаче): какая редакция/лицензия
   (Microsoft 365 по учётной записи, Office 2021/2024 с ключом, корпоративная/volume), язык (обычно ru-ru), 32 или 64 бит (по умолчанию 64).
2. Проверить, не установлен ли уже Office (PowerShell):
   `Get-ItemProperty HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*,HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\* | ? DisplayName -match 'Office|365' | select DisplayName,DisplayVersion`
   Если стоит другая версия — спросить оператора, удалять ли её.
3. Установка через официальный Office Deployment Tool (консоль от администратора, по одной команде run):
   `mkdir C:\ODT -Force | Out-Null; cd C:\ODT; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest https://officecdn.microsoft.com/pr/wsus/setup.exe -OutFile setup.exe -UseBasicParsing`
   `Set-Content config.xml '<Configuration><Add OfficeClientEdition="64" Channel="Current"><Product ID="O365BusinessRetail"><Language ID="ru-ru"/><ExcludeApp ID="Groove"/></Product></Add><Display Level="Full" AcceptEULA="TRUE"/></Configuration>'`
   `Start-Process .\setup.exe -ArgumentList '/configure config.xml' -Wait` (timeout 1800; если не успело — wait)
   Product ID по редакции: O365BusinessRetail (Microsoft 365 для бизнеса), O365ProPlusRetail (Microsoft 365 для предприятий),
   O365HomePremRetail (Microsoft 365 для семьи/персональный), HomeBusiness2024Retail / HomeStudent2024Retail (Office 2024 с ключом),
   ProPlus2021Volume с Channel="PerpetualVL2021" или ProPlus2024Volume с Channel="PerpetualVL2024" (корпоративные).
   Ключ для Office 2021/2024 можно добавить в Product: PIDKEY="XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" — ключ ТОЛЬКО от оператора.
4. Установка идёт 5–30 минут. Команда из п.3 с timeout 1800 обычно дожидается конца сама; если ответ STILL RUNNING — wait 600,
   скриншот окна установщика — только если и после этого STILL RUNNING (не ждёт ли он ответа, например «закройте программы»).
5. Проверка: повторить команду из пункта 2, затем запустить Word: `start winword`.
6. Активация: Microsoft 365 — войти в учётную запись в Word (логин/пароль спросить у оператора, лучше попросить оператора ввести пароль самому).
