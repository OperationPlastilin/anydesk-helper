"""Отчёт по тикету в Markdown: проблема, план, ход решения, команды с выводом."""

from __future__ import annotations

STATUS_RU = {"new": "новый", "running": "в работе", "waiting": "ждёт ответа", "paused": "пауза",
             "solved": "решён", "failed": "не решён", "stopped": "остановлен"}


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
            lines.append(f"- `{ts}` **Шаг {d.get('step')}** — {d.get('description', '')}"
                         + (f"  \n  _{e['text']}_" if e["text"] else ""))
        elif kind == "command":
            lines.append(f"- `{ts}` Команда: `{d.get('command', '')}` → {d.get('status')}"
                         + (f", код {d['exit_code']}" if d.get("exit_code") is not None else ""))
            if d.get("output"):
                out = d["output"].strip()
                if len(out) > 3000:
                    out = out[:3000] + "\n…"
                lines += ["", "  ```", *("  " + line for line in out.splitlines()), "  ```", ""]
        elif kind == "ask":
            lines.append(f"- `{ts}` ❓ {e['text']}")
        elif kind == "answer":
            lines.append(f"- `{ts}` ↳ {e['text']}")
        elif kind == "plan":
            lines.append(f"- `{ts}` План составлен: {e['text']}")
        else:
            lines.append(f"- `{ts}` {e['text']}")
    return "\n".join(lines) + "\n"
