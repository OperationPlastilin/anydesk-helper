keywords: интернет, internet, сет, network, wi-fi, wifi, вайфай, вай-фай, dns, днс, прокси, proxy, vpn, впн, сайт, ethernet, роутер, router
Сеть / интернет / сайты не открываются. ПК на связи через AnyDesk — значит, сеть хоть как-то работает.
- НЕ отключай и не перезапускай адаптер, не делай `ipconfig /release` — сеанс AnyDesk оборвётся. Если без этого никак — ask_operator.
- Обзор: `Get-NetIPConfiguration | select InterfaceAlias,@{n='IP';e={$_.IPv4Address.IPAddress}},@{n='GW';e={$_.IPv4DefaultGateway.NextHop}},@{n='DNS';e={$_.DNSServer.ServerAddresses -join ','}} | ft -AutoSize; Test-NetConnection ya.ru -Port 443 -InformationLevel Quiet; Resolve-DnsName ya.ru -Type A -EA 0 | select -First 2 Name,IPAddress`.
- Через системный прокси, как браузер: `[Net.ServicePointManager]::SecurityProtocol='Tls12'; (Invoke-WebRequest https://ya.ru -UseBasicParsing -TimeoutSec 20).StatusCode`.
- Прокси: `Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' | select ProxyEnable,ProxyServer,AutoConfigURL; netsh winhttp show proxy`. Лишний прокси пользователя (risky, в его консоли): `Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0`.
- IP доступен (`Test-NetConnection 77.88.8.8 -Port 53 -InformationLevel Quiet`), а имена нет — DNS: `Clear-DnsClientCache`; сменить DNS (админ, risky): `Set-DnsClientServerAddress -InterfaceAlias '<имя из вывода>' -ServerAddresses 77.88.8.8,8.8.8.8`, вернуть как было: `-ResetServerAddresses`.
- Один сайт не открывается: `Test-NetConnection <сайт> -Port 443`; файл hosts: `Get-Content $env:windir\System32\drivers\etc\hosts | ? {$_ -notmatch '^\s*(#|$)'}`.
- Адаптеры: `Get-NetAdapter | select Name,Status,LinkSpeed`; Wi-Fi: `netsh wlan show interfaces`. Имена адаптеров локализованы («Беспроводная сеть», «Ethernet») — бери из вывода.
- VPN Windows: `Get-VpnConnection | select Name,ServerAddress,ConnectionStatus` (сторонние клиенты — по их окну и службам).
- `netsh winsock reset`, `netsh int ip reset` — только после ask_operator: нужна перезагрузка.
