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
MAX_OUTPUT = 30000  # обрезаем огромный вывод ещё на той стороне


@dataclass
class CommandResult:
    status: str  # done | running | no_ack
    output: str = ""
    exit_code: int | None = None
    command_id: str = ""

    def for_model(self, limit: int = 4000) -> str:
        if self.status == "no_ack":
            return ("NO RESPONSE: the console did not receive the command. The PowerShell window "
                    "is probably not open or not focused. Look at the screenshot.")
        if self.status == "running":
            return ("STILL RUNNING (no result yet). Use wait to keep waiting, or take a "
                    "screenshot to see what is happening.")
        out = self.output.strip() or "(no output)"
        if len(out) > limit:
            out = out[: limit // 4] + "\n...[cut]...\n" + out[-(limit * 3 // 4):]
        code = "" if self.exit_code is None else f"exit code {self.exit_code}\n"
        return code + out


def ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def wrap_command(command: str, cid: str) -> str:
    """Однострочная обёртка, которую вставляем в консоль пользователя."""
    b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return (
        f"$__s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')); "
        f"Write-Host ('AI> ' + $__s) -ForegroundColor Cyan; "
        f"Set-Clipboard '{START}{cid}'; $global:LASTEXITCODE=0; "
        f"$__o = try {{ . ([ScriptBlock]::Create($__s)) *>&1 | Tee-Object -Variable __t | Out-Host; "
        f"$__t | Out-String -Width 200 }} catch {{ $_ | Out-String }}; "
        f"$__c = $LASTEXITCODE; "
        f"if ($__o.Length -gt {MAX_OUTPUT}) {{ $__o = $__o.Substring(0, 4000) + \"`n...[truncated]...`n\" "
        f"+ $__o.Substring($__o.Length - {MAX_OUTPUT - 6000}) }}; "
        f"Set-Clipboard (\"{END}{cid} exit=$__c`n\" + $__o)"
    )


def parse_clipboard(text: str, cid: str) -> CommandResult | None:
    """Разбирает содержимое буфера: None — нашего маркера нет."""
    if not text:
        return None
    if text.strip() == f"{START}{cid}":
        return CommandResult("running", command_id=cid)
    if text.startswith(f"{END}{cid}"):
        header, _, output = text.partition("\n")
        m = re.search(r"exit=(-?\d+)", header)
        return CommandResult("done", output.replace("\r\n", "\n"),
                             int(m.group(1)) if m else None, cid)
    return None


ENDUSER_DIALOG = r"""Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$f = New-Object Windows.Forms.Form -Property @{Text=TITLE; TopMost=$true; Width=480; Height=230; StartPosition='CenterScreen'; FormBorderStyle='FixedDialog'; MaximizeBox=$false; MinimizeBox=$false}
$l = New-Object Windows.Forms.Label -Property @{Text=QUESTION; Left=14; Top=12; Width=440; Height=80}
$t = New-Object Windows.Forms.TextBox -Property @{Left=14; Top=100; Width=440}
$b = New-Object Windows.Forms.Button -Property @{Text='OK'; Left=364; Top=140; Width=90; DialogResult='OK'}
$f.AcceptButton = $b; $f.Controls.AddRange(@($l, $t, $b)); $f.Add_Shown({$f.Activate(); $t.Focus()})
[void]$f.ShowDialog(); 'ANSWER: ' + $t.Text"""

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


def enduser_dialog(question: str, title: str = "Техподдержка") -> str:
    return ENDUSER_DIALOG.replace("TITLE", ps_quote(title)).replace("QUESTION", ps_quote(question))


class ConsoleBridge:
    """Выполнение команд в PowerShell пользователя. clipboard — объект с copy()/paste()."""

    def __init__(self, controller, target, clipboard=None, poll: float = 0.5,
                 ack_timeout: float = 10.0, sleep=time.sleep, clock=time.monotonic,
                 interrupted=lambda: False):
        self.controller = controller
        self.target = target
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

    def run(self, command: str, timeout: float = 120) -> CommandResult:
        if self.pending:
            return CommandResult("running", command_id=self.pending,
                                 output="previous command is still running")
        cid = uuid.uuid4().hex[:10]
        self.target.focus()
        self.controller.paste_line(wrap_command(command, cid))
        self.pending, self.started = cid, False
        return self.wait(timeout, first=True)

    def wait(self, timeout: float, first: bool = False) -> CommandResult:
        cid = self.pending
        if not cid:
            self.sleep(timeout)
            return CommandResult("done", "(no command was running; waited)")
        deadline = self.clock() + timeout
        ack_deadline = self.clock() + self.ack_timeout
        while True:
            result = parse_clipboard(self._read(), cid)
            if result and result.status == "done":
                self.pending = None
                return result
            if result:
                self.started = True
            now = self.clock()
            if first and not self.started and now >= ack_deadline:
                # Команда так и не стартовала: скорее всего, фокус не в консоли.
                self.pending = None
                return CommandResult("no_ack", command_id=cid)
            if now >= deadline or self.interrupted():
                return CommandResult("running", command_id=cid)
            self.sleep(self.poll)

    def cancel(self) -> None:
        """Ctrl+C в консоли пользователя — прервать зависшую команду."""
        self.target.focus()
        self.controller.hotkey("ctrl", "c")
        self.pending = None
