"""Консоль на удалённом ПК: команды PowerShell с выводом через буфер обмена AnyDesk.

Как это работает:
1. Мы кладём в буфер одну строку-обёртку и вставляем её в окно PowerShell у пользователя.
2. Обёртка декодирует настоящую команду (base64 — чтобы кавычки, комментарии, кириллица
   и многострочный код не ломали ввод), показывает её на экране, выполняет и
   кладёт в буфер обмена удалённого ПК маркер начала, а затем вывод и код возврата.
3. AnyDesk синхронизирует буфер обратно к нам — читаем вывод точным текстом, без OCR.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from dataclasses import dataclass

START = "AIH-RUN-"
END = "AIH-END-"
REFUSED_EXIT = -999  # обёртка попала в консоль на компьютере техника и ничего не выполнила
IDLE = "# AIH idle"  # кладём в буфер после результата: случайная повторная вставка — комментарий
MAX_OUTPUT = 30000  # обрезаем огромный вывод ещё на той стороне


@dataclass
class CommandResult:
    status: str  # done | running | no_ack | busy (не отправлена) | refused (ПК техника)
    output: str = ""
    exit_code: int | None = None
    command_id: str = ""

    def for_model(self, limit: int = 4000) -> str:
        if self.status == "no_ack":
            return ("NO RESPONSE: the console did not receive the command. The PowerShell window "
                    "is probably not open or not focused. Look at the screenshot.")
        if self.status == "running":
            return ("STILL RUNNING (no result yet). Use wait with a long interval (300-900 s), "
                    "or take a screenshot to see what is happening. If it seems stuck (the console "
                    "was closed or the result was lost), use open_terminal for a fresh console.")
        if self.status == "busy":
            return ("NOT SENT: the previous command has not reported its result yet, so this "
                    "command was not executed. Use wait to get the previous result (or "
                    "open_terminal for a fresh console if it is stuck), then send it again.")
        if self.status == "refused":
            return ("REFUSED: the command reached a PowerShell console on the technician's own PC, "
                    "not on the customer's PC, and was NOT executed.")
        out = self.output.strip() or "(no output)"
        if len(out) > limit:
            out = out[: limit // 4] + "\n...[cut]...\n" + out[-(limit * 3 // 4):]
        code = "" if self.exit_code is None else f"exit code {self.exit_code}\n"
        return code + out


def ps_quote(text: str) -> str:
    # PowerShell считает одинарными кавычками и типографские ‘ ’ ‚ ‛ — удваиваем все.
    return "'" + re.sub("['\u2018\u2019\u201a\u201b]", lambda m: m.group(0) * 2, text) + "'"


def ps_text(text: str) -> str:
    """Строка для PowerShell через base64: текст модели вообще не попадает в разбор кода."""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"


UTF16_NOTE = ("[NUL chars removed: the program printed UTF-16 text. For readable output run it as: "
              "$e=[Console]::OutputEncoding; [Console]::OutputEncoding=[Text.Encoding]::Unicode; "
              "<program>; [Console]::OutputEncoding=$e]")


def wrap_command(command: str, cid: str, local_name: str | None = None) -> str:
    """Однострочная обёртка, которую вставляем в консоль пользователя.

    - настройки ошибок/прогресса/подтверждений сбрасываются перед КАЖДОЙ командой
      (команда выполняется в общей области, и её $ErrorActionPreference='Stop' жил бы дальше);
    - вывод собирается в новый список: Tee-Object -Variable не трогает переменную, если
      команда ничего не вывела, и тогда возвращался вывод прошлой команды;
    - do {} while ($false) ловит break/continue вне цикла — иначе маркер конца не придёт;
    - NUL (UTF-16 от sfc и т.п.) убираем там же: буфер обмена обрезает текст на первом NUL;
    - local_name — имя ПК техника: если строка попала в его консоль, команда не выполняется.
    """
    b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    body = (
        f"$ErrorActionPreference='Continue'; $ProgressPreference='SilentlyContinue'; "
        f"$ConfirmPreference='None'; "
        f"$__s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')); "
        f"Write-Host ('AI> ' + $__s) -ForegroundColor Cyan; "
        f"Set-Clipboard '{START}{cid}'; $global:LASTEXITCODE=0; "
        f"$__aih=[System.Collections.Generic.List[object]]::new(); "
        f"$__o = try {{ do {{ . ([ScriptBlock]::Create($__s)) *>&1 | ForEach-Object {{ $__aih.Add($_); $_ }} "
        f"| Out-Host }} while ($false); $__aih | Out-String -Width 200 }} "
        f"catch {{ $__aih.Add($_); $__aih | Out-String -Width 200 }}; "
        f"$__c = $LASTEXITCODE; $__o = [string]$__o; "
        f"if ($__o.IndexOf([char]0) -ge 0) {{ $__o = $__o.Replace([string][char]0, '') + \"`n\" + "
        f"{ps_quote(UTF16_NOTE)} }}; "
        f"if ($__o.Length -gt {MAX_OUTPUT}) {{ $__o = $__o.Substring(0, 4000) + \"`n...[truncated]...`n\" "
        f"+ $__o.Substring($__o.Length - {MAX_OUTPUT - 6000}) }}; "
        f"Set-Clipboard (\"{END}{cid} exit=$__c`n\" + $__o)"
    )
    if not local_name:
        return body
    return (f"if ($env:COMPUTERNAME -eq {ps_text(local_name)}) {{ Write-Host 'AnyDesk Helper: "
            f"this command was meant for the remote PC and was NOT run here' -ForegroundColor Red; "
            f"Set-Clipboard \"{END}{cid} exit={REFUSED_EXIT}`nREFUSED: technician PC\" }} "
            f"else {{ {body} }}")


def parse_clipboard(text: str, cid: str) -> CommandResult | None:
    """Разбирает содержимое буфера: None — нашего маркера нет."""
    if not text:
        return None
    if text.strip() == f"{START}{cid}":
        return CommandResult("running", command_id=cid)
    if text.startswith(f"{END}{cid}"):
        header, _, output = text.partition("\n")
        m = re.search(r"exit=(-?\d+)", header)
        code = int(m.group(1)) if m else None
        output = output.replace("\r\n", "\n")
        status = "refused" if code == REFUSED_EXIT and output.startswith("REFUSED") else "done"
        return CommandResult(status, output, code, cid)
    return None


# Переменные с префиксом $__d: скрипт выполняется в общей области и не должен затирать
# переменные модели ($f, $t…). Таймер закрывает окно, если пользователь отошёл: иначе консоль
# занята навсегда, а Ctrl+C модальное окно не закрывает.
ENDUSER_DIALOG = r"""Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$__df = New-Object Windows.Forms.Form -Property @{Text=TITLE; TopMost=$true; Width=480; Height=230; StartPosition='CenterScreen'; FormBorderStyle='FixedDialog'; MaximizeBox=$false; MinimizeBox=$false}
$__dl = New-Object Windows.Forms.Label -Property @{Text=QUESTION; Left=14; Top=12; Width=440; Height=80}
$__dt = New-Object Windows.Forms.TextBox -Property @{Left=14; Top=100; Width=440}
$__db = New-Object Windows.Forms.Button -Property @{Text='OK'; Left=364; Top=140; Width=90; DialogResult='OK'}
$__df.AcceptButton = $__db; $__df.Controls.AddRange(@($__dl, $__dt, $__db)); $__df.Add_Shown({$__df.Activate(); $__dt.Focus()})
$__dm = New-Object Windows.Forms.Timer -Property @{Interval=TIMEOUT_MS}; $__dm.Add_Tick({$__dm.Stop(); $__df.Close()}); $__dm.Start()
$__dr = $__df.ShowDialog(); $__dm.Stop(); $__dm.Dispose(); $__da = $__dt.Text; $__df.Dispose()
if ($__dr -eq 'OK' -or $__da) { 'ANSWER: ' + $__da } else { 'ANSWER: (no answer: the customer closed the dialog or did not reply in time)' }"""

PROBE = (
    "$o = Get-CimInstance Win32_OperatingSystem; "
    "$adm = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
    ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); "
    "\"PC: $env:COMPUTERNAME; user: $env:USERDOMAIN\\$env:USERNAME; OS: $($o.Caption) $($o.Version) "
    "$($o.OSArchitecture); PowerShell: $($PSVersionTable.PSVersion); admin: $adm; "
    "winget: $([bool](Get-Command winget -ErrorAction SilentlyContinue)); "
    "free C: $([math]::Round((Get-PSDrive C).Free/1GB,1)) GB\""
)

INIT = ("$ProgressPreference='SilentlyContinue'; $ConfirmPreference='None'; "
        "$host.UI.RawUI.WindowTitle='AI Helper console'")


def enduser_dialog(question: str, title: str = "Техподдержка", timeout_s: int = 240) -> str:
    """Окно с вопросом на экране пользователя. Закрывается само через timeout_s секунд —
    ждать результат команды нужно дольше (agent: timeout=300)."""
    return (ENDUSER_DIALOG.replace("TIMEOUT_MS", str(int(timeout_s * 1000)))
            .replace("TITLE", ps_text(title)).replace("QUESTION", ps_text(question)))


class ConsoleBridge:
    """Выполнение команд в PowerShell пользователя. clipboard — объект с copy()/paste()."""

    def __init__(self, controller, target, clipboard=None, poll: float = 0.5,
                 ack_timeout: float = 10.0, sleep=time.sleep, clock=time.monotonic,
                 interrupted=lambda: False, local_name: str | None = None):
        self.controller = controller
        self.target = target
        self.local_name = local_name  # имя ПК техника (режим AnyDesk): там команды не выполняются
        self.clipboard = clipboard
        self.poll = poll
        self.ack_timeout = ack_timeout
        self.sleep = sleep
        self.clock = clock
        self.interrupted = interrupted  # техник нажал «Стоп» — не ждём до конца таймаута
        self.pending: str | None = None  # id команды, которая ещё выполняется
        self.started = False

    def _clip(self):
        if self.clipboard is None:
            import pyperclip

            self.clipboard = pyperclip
        return self.clipboard

    def _read(self) -> str:
        try:
            return self._clip().paste() or ""
        except Exception:
            return ""  # буфер занят другим процессом (часто, когда AnyDesk его обновляет)

    def _idle(self) -> None:
        """Результат забран — заменяем его в буфере комментарием. Иначе, если AnyDesk не успеет
        передать следующую обёртку, Ctrl+V+Enter выполнит в консоли вывод прошлой команды."""
        try:
            self._clip().copy(IDLE)
        except Exception:
            pass

    def run(self, command: str, timeout: float = 120) -> CommandResult:
        if self.pending:
            # Новую команду не отправляем: модель должна знать, что она НЕ выполнялась.
            return CommandResult("busy", command_id=self.pending)
        cid = uuid.uuid4().hex[:10]
        self.target.focus()
        # FocusError из paste_line после Ctrl+V (до Enter): pending не ставим — команда не
        # запущена; Controller запомнит недописанную строку и перед повтором очистит её (Esc).
        self.controller.paste_line(wrap_command(command, cid, self.local_name))
        self.pending, self.started = cid, False
        return self.wait(timeout, first=True)

    def wait(self, timeout: float, first: bool = False) -> CommandResult:
        cid = self.pending
        if not cid:
            self.sleep(timeout)
            return CommandResult("done", "(no command was running; waited)")
        deadline = self.clock() + timeout
        ack_deadline = self.clock() + self.ack_timeout
        if first:
            # Короткий таймаут не должен пропускать no_ack: иначе команда, которая не дошла до
            # консоли, навсегда останется «выполняющейся».
            deadline = max(deadline, ack_deadline)
        while True:
            result = parse_clipboard(self._read(), cid)
            if result and result.status in ("done", "refused"):
                self.pending = None
                self._idle()
                return result
            if result:
                self.started = True
            now = self.clock()
            if first and not self.started and now >= ack_deadline:
                # Команда так и не стартовала: скорее всего, фокус не в консоли.
                self.pending = None
                self._idle()  # в буфере осталась обёртка — не дать вставить её ещё раз
                return CommandResult("no_ack", command_id=cid)
            if now >= deadline or self.interrupted():
                return CommandResult("running", command_id=cid)
            self.sleep(self.poll)

    def cancel(self) -> None:
        """Ctrl+C в консоли пользователя — прервать зависшую команду. pending сбрасываем сразу:
        если окно AnyDesk не вывести вперёд (anydesk.FocusError), команда всё равно забыта."""
        self.pending = None
        self.target.focus()
        self.controller.hotkey("ctrl", "c")
        self._idle()
