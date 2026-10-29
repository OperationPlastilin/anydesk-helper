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

# Команды, которые всегда требуют подтверждения оператора в режиме confirm=risky.
DANGEROUS_PATTERNS = [
    r"\bremove-item\b", r"\brm\s", r"\bdel\s", r"\berase\b", r"\brd\s", r"\brmdir\b",
    r"\bformat(-volume)?\b", r"\bdiskpart\b", r"\bbcdedit\b", r"\breg\s+(delete|add|import)\b",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b", r"\bset-executionpolicy\b",
    r"\bcipher\b", r"\bclear-disk\b", r"\bremove-partition\b", r"\bnet\s+user\b",
    r"\buninstall\b", r"\bremove-appxpackage\b", r"\bvssadmin\b", r"\bwmic\b.*\bdelete\b",
    r"\bremove-localuser\b", r"\bdisable-", r"\bstop-service\b", r"\bsc\s+(delete|stop|config)\b",
    r"\binstall", r"\bmsiexec\b", r"\bsetup\.exe\b", r"\bset-itemproperty\b",
    r"\bnew-itemproperty\b", r"\bremove-", r"\bset-service\b", r"\bnetsh\b",
    r"-verb\s+runas\b", r"\btakeown\b", r"\bicacls\b", r"\bset-mppreference\b",
]
DANGEROUS_HOTKEYS = [{"alt", "f4"}, {"ctrl", "alt", "delete"}, {"win", "l"}]


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
    timeout: float = 120.0
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
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
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


def parse_action(data: dict, img_w: int = 0, img_h: int = 0, coords: str = "pixels") -> Action:
    """Проверяет JSON от модели. Координаты возвращаются в пикселях скриншота.

    img_w/img_h = 0 означает, что в этом шаге скриншота не было — GUI-клики запрещены.
    """
    kind = str(data.get("action") or data.get("type") or "").strip().lower()
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
            raise ActionError("'command' is empty")
        if "timeout" in data:
            a.timeout = min(max(_number(data, "timeout"), 5.0), 1800.0)
    if kind == "wait":
        a.seconds = min(max(_number(data, "seconds"), 1.0), 600.0)
    if kind in ("ask_operator", "ask_enduser", "done", "fail"):
        key = {"done": "summary", "fail": "reason"}.get(kind, "question")
        a.text = str(data.get(key) or data.get("text") or "").strip()
        if kind.startswith("ask") and not a.text:
            raise ActionError("'question' is empty")
    return a


def is_dangerous_text(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in DANGEROUS_PATTERNS)


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
