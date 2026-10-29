keywords: обновлени, обновить windows, обновить винд, windows update, wuauserv, kb5, 0x8024, 0x800f, sfc, dism, системные файл, системных файл
Windows Update и восстановление системных файлов — в консоли администратора.
- Состояние: `Get-Service wuauserv,bits,cryptsvc,trustedinstaller | select Name,Status,StartType; Get-HotFix | sort InstalledOn -Desc | select -First 5 HotFixID,InstalledOn; Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'` (True — сначала перезагрузка: ask_enduser, risky).
- Ошибки: `Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WindowsUpdateClient';Level=2,3} -MaxEvents 5 -EA 0 | select TimeCreated,Id,Message | fl`.
- Что ждёт установки (timeout 600): `(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0').Updates | select Title`.
- Сброс кэша обновлений (risky): `Stop-Service wuauserv,bits,cryptsvc -Force; Rename-Item C:\Windows\SoftwareDistribution SoftwareDistribution.old; Rename-Item C:\Windows\System32\catroot2 catroot2.old; Start-Service wuauserv,bits,cryptsvc`. Папка .old уже есть — сначала удали её; «занята» — служба запустилась снова, повтори.
- Восстановление (нужен интернет; по одной команде run с timeout 1800, если не успело — wait): сначала `DISM /Online /Cleanup-Image /RestoreHealth | select -Last 5`, затем sfc /scannow
  по рецепту из general (вывод sfc в UTF-16). В журнале CBS.log «[SR] Cannot repair» — повреждения остались: скажи оператору.
- Ручная установка .msu: `wusa <файл>.msu /quiet /norestart` (risky).
