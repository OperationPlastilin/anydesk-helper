"""Отчёт по тикету в Markdown: проблема, план, ход решения, команды с выводом."""

from __future__ import annotations

import re

STATUS_RU = {"new": "новый", "running": "в работе", "waiting": "ждёт ответа", "paused": "пауза",
             "solved": "решён", "failed": "не решён", "stopped": "остановлен"}


def _block(text: str, info: str = "") -> list[str]:
    """Кодблок внутри пункта списка. Ограда длиннее любой серии ` в тексте: обратная кавычка
    PowerShell (`n, перенос строки) и ``` в выводе не закрывают блок раньше времени."""
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return ["", f"  {fence}{info}", *("  " + line for line in text.splitlines()), f"  {fence}", ""]


def _item(text: str) -> str:
    """Многострочный текст остаётся внутри пункта списка (строка «# …» не станет заголовком)."""
    return (text or "").replace("\n", "\n  ")


def ticket_report(t: dict, events: list[dict]) -> str:
    lines = [f"# Тикет #{t['id']}: {t['title']}", "",
             f"- Статус: {STATUS_RU.get(t['status'], t['status'])}",
             f"- AnyDesk: {t['anydesk_id'] or '—'}",
             f"- Создан: {t['created_at']}, обновлён: {t['updated_at']}", ""]
    if t["description"]:
        lines += ["## Описание проблемы", "", t["description"], ""]
    if t["plan"]:
        lines += ["## План", ""] + [f"{i}. {p}" for i, p in enumerate(t["plan"], 1)] + [""]
    if t["summary"]:
        lines += ["## Итог", "", t["summary"], ""]
    lines += ["## Ход решения", ""]
    for e in events:
        d, ts = e["data"], e["ts"][11:]
        kind = e["kind"]
        if kind == "prompt":
            continue
        if kind == "step":
            lines.append(_item(f"- `{ts}` **Шаг {d.get('step')}** — {d.get('description', '')}"
                               + (f"  \n_{e['text']}_" if e["text"] else "")))
        elif kind == "command":
            lines.append(f"- `{ts}` Команда → {d.get('status')}"
                         + (f", код {d['exit_code']}" if d.get("exit_code") is not None else ""))
            lines += _block(str(d.get("command", "")), "powershell")
            if d.get("output"):
                out = d["output"].strip()
                if len(out) > 3000:
                    out = out[:3000] + "\n…"
                lines += _block(out)
        elif kind == "ask":
            lines.append(_item(f"- `{ts}` ❓ {e['text']}"))
        elif kind == "answer":
            lines.append(_item(f"- `{ts}` ↳ {e['text']}"))
        elif kind == "plan":
            lines.append(_item(f"- `{ts}` План составлен: {e['text']}"))
        else:
            lines.append(_item(f"- `{ts}` {e['text']}"))
    return "\n".join(lines) + "\n"
