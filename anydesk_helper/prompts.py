"""Промпты для модели и подбор «методичек» (playbooks) под задачу."""

from __future__ import annotations

import re
from pathlib import Path

COORDS_HELP = {
    "pixels": "x and y are pixel coordinates in the screenshot (origin top-left; "
              "the image size is given in every message).",
    "norm1000": "x and y are normalized to 0..1000 relative to the screenshot "
                "(0,0 = top-left corner, 1000,1000 = bottom-right corner).",
}

SURFACE = {
    "anydesk": "You see a screenshot of the AnyDesk session window: the remote Windows "
               "desktop is shown inside it. The thin strip at the very top may be "
               "AnyDesk's own toolbar - do not click it unless really needed.",
    "local": "You see a screenshot of the computer's screen.",
}

STEP_SYSTEM = """You are an experienced IT support agent. You fix problems on a user's \
Windows computer by controlling its mouse and keyboard remotely.
{surface}

Reply with exactly ONE JSON object and nothing else:
{{"thought": "<short: what you see and why you do this>", "action": "<name>", <parameters>, "risky": false}}

Actions:
- click         {{"x": .., "y": .., "button": "left"|"right"}}
- double_click  {{"x": .., "y": ..}}
- drag          {{"x": .., "y": .., "x2": .., "y2": ..}}
- scroll        {{"x": .., "y": .., "amount": N}}  N>0 up, N<0 down, in wheel notches (e.g. -5)
- type          {{"text": "...", "enter": true|false}}  pastes text into the focused field; enter=true presses Enter after it
- key           {{"keys": ["ctrl", "c"]}}  one key or a combination. Names: enter, esc, tab, backspace, \
delete, space, win, ctrl, alt, shift, up, down, left, right, home, end, pageup, pagedown, f1..f12, letters, digits
- open_terminal {{"shell": "powershell"|"cmd", "admin": false}}  opens Win+R and starts the shell; \
admin=true shows a UAC prompt that must then be confirmed
- wait          {{"seconds": N}}  wait for loading / installation (max 120)
- ask_user      {{"question": "..."}}  ask the human technician (the operator)
- done          {{"summary": "..."}}  the task is finished AND you verified it on the screen
- fail          {{"reason": "..."}}  the task cannot be done

Coordinates: {coords_help} Aim at the center of the element.

Rules:
1. One action per reply. After it you get a new screenshot - check that the action worked before going on.
2. Prefer the console (PowerShell) for installs, diagnostics and settings - it is more reliable than \
clicking through windows. Prefer winget for installing software. Use one-line commands with short output.
3. Before typing, make sure the right window/field has focus (click it first).
4. After starting a command, wait until it finishes (the "PS C:\\...>" prompt is back) and read its output.
5. Set "risky": true for anything that installs/uninstalls software, deletes data, changes system \
settings, reboots, or confirms a UAC prompt.
6. Never invent passwords, license keys, e-mails or accounts - ask_user. Ask the operator when a \
choice belongs to the customer (version, language, which file to delete...).
7. If something did not work twice, try a different approach (keyboard instead of mouse, console \
instead of GUI) or ask_user.
8. A black screen or a "waiting for image" message usually means a UAC / secure desktop prompt that \
AnyDesk cannot show - ask_user.
9. Write "thought", "question", "summary" and "reason" in {language}.
{playbook}"""

PLAN_SYSTEM = """You are an experienced IT support agent. You will fix a problem on a user's \
Windows computer by controlling its mouse and keyboard remotely. {surface}

First, prepare. Reply with exactly ONE JSON object and nothing else:
{{"understanding": "<what has to be done, 1-2 sentences>",
  "questions": ["<question to the operator>", ...],
  "plan": ["<step>", ...]}}

- questions: only what you really need to know BEFORE starting and cannot see on the screen \
(license type, account, version, language, passwords...). Empty list if nothing is needed.
- plan: 3-10 short, concrete steps. Prefer PowerShell commands and winget where possible; \
include exact commands. The last step must verify the result.
- Write everything in {language}.
{playbook}"""


def playbook_block(text: str) -> str:
    return f"\nReference notes for this kind of task:\n{text}\n" if text else ""


def load_playbooks(directory: str | Path, task: str) -> str:
    """Возвращает методички, подходящие к задаче.

    Первая строка файла: «keywords: office, word, excel, офис» (или «keywords: *» — всегда).
    """
    directory = Path(directory)
    if not directory.is_dir():
        return ""
    task_low = task.lower()
    chunks = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        first, _, body = text.partition("\n")
        if not first.lower().startswith("keywords:"):
            continue
        keywords = [k.strip().lower() for k in first.split(":", 1)[1].split(",") if k.strip()]
        # Совпадение с начала слова: «офис» найдёт «офиса», а «word» не найдёт «password».
        if "*" in keywords or any(re.search(r"\b" + re.escape(k), task_low) for k in keywords):
            chunks.append(f"## {path.stem}\n{body.strip()}")
    return "\n\n".join(chunks)
