"""Разбор ответа модели в действие и проверка его корректности."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

CONSOLE_ACTIONS = ("open_terminal", "run", "wait", "ask_enduser")
GUI_ACTIONS = ("screenshot", "click", "double_click", "right_click", "drag", "scroll", "type", "key")
TALK_ACTIONS = ("ask_operator", "done", "fail")
ACTION_TYPES = CONSOLE_ACTIONS + GUI_ACTIONS + TALK_ACTIONS
ALIASES = {"ask_user": "ask_operator", "ask": "ask_operator", "command": "run", "powershell": "run",
           "shell": "run", "exec": "run", "finish": "done", "look": "screenshot"}
POINTER_ACTIONS = ("click", "double_click", "right_click", "drag", "scroll")

KEY_ALIASES = {
    "windows": "win", "super": "win", "meta": "win", "cmd": "win",
    "control": "ctrl", "return": "enter", "escape": "esc", "del": "delete",
    "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup",
    "page_down": "pagedown", "space": "space", "spacebar": "space",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "bksp": "backspace", "prtsc": "printscreen",
}
NAMED_KEYS = {
    "enter", "esc", "tab", "backspace", "delete", "insert", "space", "win", "ctrl",
    "alt", "shift", "up", "down", "left", "right", "home", "end", "pageup",
    "pagedown", "capslock", "printscreen", "apps",
} | {f"f{i}" for i in range(1, 13)}

# Загрузка и запуск кода из сети. Уроки с таким решением агент не включает без проверки техником.
REMOTE_CODE_PATTERNS = [
    r"https?://", r"\b(iex|invoke-expression)\b", r"\[scriptblock\]::create",
    r"\b(iwr|irm|curl|wget|invoke-webrequest|invoke-restmethod|start-bitstransfer|bitsadmin)\b",
    r"download(string|file|data)\b", r"\bcertutil\b.*-urlcache", r"\b(mshta|regsvr32|rundll32)\b",
    # powershell -e/-ec/-enc…/-EncodedCommand <base64> — только префиксы EncodedCommand, не -Encoding
    r"\s-e(c|n(c(o(d(e(d(c\w*)?)?)?)?)?)?)?\s+[a-z0-9+/=]{16,}",
]
# Команды, которые всегда требуют подтверждения оператора в режиме confirm=risky.
# Только чтение (Format-Table, ветка реестра ...\Uninstall, Get-InstalledModule, netsh … show) — без вопросов.
DANGEROUS_PATTERNS = [
    r"\bremove-item\b", r"\brm\s", r"\bdel\s", r"\berase\b", r"\brd\s", r"\brmdir\b",
    r"\bformat-volume\b", r"(?<![-.\w])format(\.com)?\s+[a-z]:", r"\bdiskpart\b", r"\bbcdedit\b",
    r"\breg\s+(delete|add|import)\b",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b", r"\bset-executionpolicy\b",
    r"\bcipher\b", r"\bclear-disk\b", r"\bremove-partition\b", r"\bnet\s+user\b",
    r"(?<!currentversion\\)uninstall", r"\bremove-appxpackage\b", r"\bvssadmin\b", r"\bwmic\b.*\bdelete\b",
    r"\bremove-localuser\b", r"\bdisable-", r"\bstop-service\b",
    r"\bsc(\.exe)?\s+(create|delete|stop|config)\b",
    r"\binstall(?!ed|date|location|source|state|time)",  # Install-Module, install_x64.exe, InstallUtil
    r"\badd-appxpackage\b", r"\bmsiexec\b", r"\bsetup\.exe\b", r"\bset-itemproperty\b",
    r"\bnew-itemproperty\b", r"\bremove-", r"\b(new|set)-service\b",
    r"\bnetsh\b(?!\s+(?:[\w-]+\s+){0,3}show\b)", r"\bkey\s*=\s*clear\b",
    r"-verb\s+runas\b", r"\btakeown\b", r"\bicacls\b", r"\b(add|set)-mppreference\b",
    r"\b(stop-process|spps|kill|taskkill|tskill)\b",  # закрыть программы — пользователь потеряет работу
    r"\bnet\s+localgroup\b.*/(add|delete)", r"\b(add|new|set)-local(user|group(member)?)\b",
    r"\bschtasks\b.*/(create|change|delete)", r"\b(register|set|unregister)-scheduledtask\b",
    r"\b(new|set)-netfirewall", r"\b(rename|add)-computer\b", r"\bclear-recyclebin\b",
    *REMOTE_CODE_PATTERNS,
]
DANGEROUS_HOTKEYS = [{"alt", "f4"}, {"ctrl", "alt", "delete"}, {"win", "l"}]
# exit в начале оператора: обёртка выполняет команду в консоли пользователя (dot-source), и exit
# закрыл бы её окно — результат не придёт никогда. «cmd /c exit 3», Exit-PSSession, $LASTEXITCODE — можно.
EXIT_RE = re.compile(r"(?im)(?:^|[;{}(])\s*exit(?![\w-])|\[(?:system\.)?environment\]::exit\b")


class ActionError(ValueError):
    """Ответ модели не удалось превратить в действие. Текст уходит обратно модели."""


@dataclass
class Action:
    type: str
    observation: str = ""
    plan_step: int | None = None
    thought: str = ""
    x: float | None = None  # в пикселях отправленного скриншота
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    text: str = ""  # текст для type, вопрос, итог или причина неудачи
    command: str = ""
    timeout: float | None = None  # None — таймаут из настроек (command_timeout)
    keys: list[str] = field(default_factory=list)
    amount: int = 0
    seconds: float = 0.0
    admin: bool = False
    enter: bool = False
    risky: bool = False

    def signature(self) -> tuple:
        """Для обнаружения зацикливания: одинаковые действия дают одинаковую подпись."""
        point = (round(self.x or 0, -1), round(self.y or 0, -1))
        return (self.type, point, self.text, self.command, tuple(self.keys))

    def describe(self) -> str:
        t = self.type
        if t in ("click", "double_click", "right_click"):
            return f"{t} ({self.x:.0f}, {self.y:.0f})"
        if t == "drag":
            return f"drag ({self.x:.0f}, {self.y:.0f}) -> ({self.x2:.0f}, {self.y2:.0f})"
        if t == "scroll":
            return f"scroll {self.amount:+d} at ({self.x:.0f}, {self.y:.0f})"
        if t == "type":
            return f"type {self.text!r}" + (" + Enter" if self.enter else "")
        if t == "key":
            return "key " + "+".join(self.keys)
        if t == "open_terminal":
            return "open_terminal" + (" (admin)" if self.admin else "")
        if t == "run":
            return f"run: {self.command}"
        if t == "wait":
            return f"wait {self.seconds:g}s"
        if t == "screenshot":
            return "screenshot"
        return f"{t}: {self.text}"


def extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (с ```json, <think> и мусором вокруг)."""
    if not text:
        raise ActionError("Empty reply")
    # Рассуждение — всё до последнего </think>: у Qwen3-Thinking/QwQ открывающий тег стоит в самом
    # промте, и в ответе есть только закрывающий. Черновой JSON из рассуждения выполнять нельзя.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif text.lstrip().startswith("<think>"):
        raise ActionError("Reply was cut off inside <think> reasoning. Think briefly and answer "
                          "with the JSON object")
    decoder = json.JSONDecoder()
    error = None
    for match in re.finditer(r"\{", text):
        try:
            obj, end = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError as e:
            error = error or e
            continue
        if not isinstance(obj, dict) or not obj:
            continue  # {} из текста вроде «screenshot {}» — не ответ
        if error and text[end:].lstrip()[:1] in (",", "}"):
            continue  # вложенный объект из испорченного внешнего — тоже не ответ
        return obj
    if error:
        hint = (' In JSON strings write every backslash doubled ("C:\\\\Users") or use forward '
                "slashes (C:/Users)." if "escape" in error.msg else "")
        raise ActionError(f"Invalid JSON: {error.msg} at char {error.pos}.{hint}")
    raise ActionError("No valid JSON object found in the reply")


def _number(data: dict, key: str) -> float:
    value = data.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ActionError(f"'{key}' must be a number, got {value!r}") from None


def _point(data: dict, xkey: str, ykey: str) -> tuple[float, float]:
    if xkey in data and ykey in data:
        return _number(data, xkey), _number(data, ykey)
    # Qwen часто отвечает в своём «родном» формате: [x, y] или bbox [x1, y1, x2, y2].
    for key in ("coordinate", "point", "position", "bbox_2d", "bbox"):
        value = data.get(key)
        if isinstance(value, list) and len(value) in (2, 4) and xkey == "x":
            try:
                nums = [float(v) for v in value]
            except (TypeError, ValueError):
                continue
            if len(nums) == 2:
                return nums[0], nums[1]
            return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    raise ActionError(f"Action needs '{xkey}' and '{ykey}' coordinates")


def normalize_keys(raw) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"\s*\+\s*", raw.strip()) if raw.strip() != "+" else ["+"]
    elif isinstance(raw, list):
        parts = [str(k) for k in raw]
    else:
        raise ActionError("'keys' must be a list like [\"ctrl\", \"c\"]")
    keys = []
    for part in parts:
        key = part.strip().lower()
        key = KEY_ALIASES.get(key, key)
        if not key:
            continue
        if key not in NAMED_KEYS and len(key) != 1:
            raise ActionError(f"Unknown key {part!r}")
        keys.append(key)
    if not keys:
        raise ActionError("'keys' is empty")
    return keys


WRAPPER_KEYS = ("parameters", "params", "args", "arguments", "action_input", "input")


def _flatten(data: dict) -> dict:
    """Qwen любит вкладывать параметры: {"action": {"type": "run", ...}}, {"action": "run",
    "parameters": {...}}, {"name": "run", "arguments": "{...}"}. Поднимаем их наверх; ключи
    верхнего уровня главнее (вложенный risky=false не отменит risky=true)."""
    data = dict(data)
    inner = data.get("action")
    if isinstance(inner, dict):
        data["action"] = inner.get("type") or inner.get("name") or inner.get("action") or ""
        for k, v in inner.items():
            data.setdefault(k, v)
    name = str(data.get("action") or data.get("type") or data.get("name") or "")
    for key in (*WRAPPER_KEYS, name):
        value = data.get(key)
        if key == "arguments" and isinstance(value, str):  # стиль function calling: JSON строкой
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            for k, v in value.items():
                data.setdefault(k, v)
    return data


def parse_action(data: dict, img_w: int = 0, img_h: int = 0, coords: str = "pixels") -> Action:
    """Проверяет JSON от модели. Координаты возвращаются в пикселях скриншота.

    img_w/img_h = 0 означает, что в этом шаге скриншота не было — GUI-клики запрещены.
    """
    data = _flatten(data)
    kind = str(data.get("action") or data.get("type") or data.get("name")
               or ("run" if data.get("command") or data.get("cmd") else "")).strip().lower()
    kind = ALIASES.get(kind, kind)
    if kind not in ACTION_TYPES:
        raise ActionError(f"Unknown action {kind!r}. Allowed: {', '.join(ACTION_TYPES)}")
    step = data.get("plan_step")
    a = Action(type=kind,
               observation=str(data.get("observation", "")).strip(),
               plan_step=int(step) if isinstance(step, (int, float)) or str(step).isdigit() else None,
               thought=str(data.get("thought", "")).strip(),
               risky=bool(data.get("risky", False)))

    def to_px(x: float, y: float) -> tuple[float, float]:
        if coords == "norm1000":
            x, y = x / 1000 * img_w, y / 1000 * img_h
        if not (0 <= x <= img_w and 0 <= y <= img_h):
            raise ActionError(f"Point ({x:.0f}, {y:.0f}) is outside the screenshot")
        return x, y

    if kind in POINTER_ACTIONS:
        if not img_w:
            raise ActionError("You have no screenshot in this step. Use the screenshot action "
                              "first, then click on what you see.")
        a.x, a.y = to_px(*_point(data, "x", "y"))
    if kind == "drag":
        a.x2, a.y2 = to_px(*_point(data, "x2", "y2"))
    if kind == "click" and str(data.get("button", "left")).lower() == "right":
        a.type = "right_click"
    if kind == "scroll":
        a.amount = int(_number(data, "amount"))
        if a.amount == 0:
            raise ActionError("'amount' must not be 0")
    if kind == "type":
        a.text = str(data.get("text", ""))
        if not a.text:
            raise ActionError("'text' is empty")
        a.enter = bool(data.get("enter", False))
    if kind == "key":
        a.keys = normalize_keys(data.get("keys", data.get("key")))
    if kind == "open_terminal":
        a.admin = bool(data.get("admin", False))
    if kind == "run":
        a.command = str(data.get("command") or data.get("cmd") or "").strip()
        if not a.command:
            raise ActionError("'command' is empty: put \"command\" at the top level, next to \"action\"")
        if EXIT_RE.search(a.command):
            raise ActionError("Do not use exit: it closes the customer's PowerShell window. Use return "
                              "to stop early or throw to report an error; use open_terminal for a fresh console.")
        if "timeout" in data:
            a.timeout = min(max(_number(data, "timeout"), 5.0), 1800.0)
    if kind == "wait":
        a.seconds = min(max(_number(data, "seconds"), 1.0), 1800.0)
    if kind in ("ask_operator", "ask_enduser", "done", "fail"):
        key = {"done": "summary", "fail": "reason"}.get(kind, "question")
        a.text = str(data.get(key) or data.get("text") or "").strip()
        if kind.startswith("ask") and not a.text:
            raise ActionError("'question' is empty")
    return a


def is_dangerous_text(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in DANGEROUS_PATTERNS)


def is_remote_code(text: str) -> bool:
    """Ссылка или загрузка/запуск кода из сети (irm … | iex, DownloadString, -enc …)."""
    low = (text or "").lower()
    return any(re.search(p, low) for p in REMOTE_CODE_PATTERNS)


def needs_confirmation(action: Action, mode: str, last_typed: str = "") -> bool:
    if action.type in ("ask_operator", "ask_enduser", "done", "fail", "wait", "screenshot"):
        return False
    if mode == "all":
        return True
    if mode == "none":
        return False
    if action.risky:
        return True
    if action.type == "open_terminal" and action.admin:
        return True
    if action.type == "run" and is_dangerous_text(action.command):
        return True
    if action.type == "type" and is_dangerous_text(action.text):
        return True
    if action.type == "key":
        pressed = set(action.keys)
        if pressed == {"enter"} and last_typed and is_dangerous_text(last_typed):
            return True
        if any(combo <= pressed for combo in DANGEROUS_HOTKEYS):
            return True
    return False
