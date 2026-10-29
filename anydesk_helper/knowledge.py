"""База опыта: уроки из прошлых тикетов — что сработало, какие были ошибки и как их чинили.

Как агент учится:
1. После сеанса модель разбирает ход решения и формулирует 0–3 урока (рефлексия).
2. Похожие уроки не плодятся: новый урок сливается с существующим (seen += 1).
3. Перед планом нового тикета агент ищет похожий опыт и кладёт его в промт.
4. Когда команда падает с ошибкой, агент ищет урок по тексту ошибки и подсказывает модели.
5. Счётчики uses/wins: сколько раз урок подсказывали и сколько из этих тикетов решено —
   полезные уроки поднимаются в поиске выше.
"""

from __future__ import annotations

import json
import math
import re

from .db import now

KINDS = ("solution", "error_fix", "pitfall", "manual")
KIND_RU = {"solution": "рецепт", "error_fix": "ошибка → решение", "pitfall": "не делать",
           "manual": "от техника"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    problem       TEXT NOT NULL DEFAULT '',
    solution      TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    source_ticket INTEGER,
    uses          INTEGER NOT NULL DEFAULT 0,
    wins          INTEGER NOT NULL DEFAULT 0,
    seen          INTEGER NOT NULL DEFAULT 1,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lesson_uses (
    ticket_id INTEGER NOT NULL,
    lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    PRIMARY KEY (ticket_id, lesson_id)
);
"""

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "not", "you", "are", "was", "has",
    "для", "что", "как", "это", "или", "при", "его", "она", "они", "все", "уже", "нет", "без",
    "нужно", "надо", "пользователь", "пользователя", "компьютер", "компьютера", "чтобы", "если",
}
ERROR_RE = re.compile(r"error|exception|failed|denied|cannot|not found|не удалось|ошибка|"
                      r"отказано|не найден|невозможно|is not recognized", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SECRET_RE = re.compile(r"(?i)(-password|password\s*[:=]|пароль\s*[:=]?|pidkey\s*=|/inpkey:)\s*\S+")
KEY_RE = re.compile(r"\b[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}\b")  # ключи продуктов


def tokens(text: str) -> set[str]:
    """Слова текста с грубым «стеммингом» (первые 6 букв): «принтера» ~ «принтер»."""
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9_-]{2,}", (text or "").lower())
    return {w[:6] for w in words if w not in STOP}


def looks_like_error(output: str, exit_code) -> bool:
    return bool(exit_code) or bool(ERROR_RE.search(output or ""))


def scrub(text: str) -> str:
    """Уроки не должны хранить персональные данные и секреты пользователей."""
    text = EMAIL_RE.sub("<email>", text or "")
    text = KEY_RE.sub("<ключ>", text)
    return SECRET_RE.sub(lambda m: m.group(1) + " <скрыто>", text)


class KnowledgeBase:
    def __init__(self, db):
        self.db = db
        with db.lock, db.conn:
            db.conn.executescript(SCHEMA)

    # ------------------------------------------------------------ чтение

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["tags"] = [t for t in d["tags"].split(",") if t]
        d["enabled"] = bool(d["enabled"])
        return d

    def get(self, lesson_id: int) -> dict | None:
        with self.db.lock:
            row = self.db.conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, query: str = "", kind: str = "") -> list[dict]:
        sql, args = "SELECT * FROM lessons WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        with self.db.lock:
            rows = [self._row(r) for r in self.db.conn.execute(sql + " ORDER BY updated_at DESC", args)]
        if query:
            scored = [(self._score(tokens(query), r), r) for r in rows]
            rows = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]
        return rows

    def _score(self, q: set[str], lesson: dict) -> float:
        body = tokens(" ".join([lesson["title"], lesson["problem"], *lesson["tags"]]))
        body |= {t for t in tokens(lesson["solution"]) if len(t) >= 4}
        if not q or not body:
            return 0.0
        overlap = len(q & body) / math.sqrt(len(q) * len(body))
        return overlap * (1 + 0.1 * min(lesson["wins"], 5))

    def search(self, text: str, limit: int = 5, min_score: float = 0.2, exclude=()) -> list[dict]:
        q = tokens(text)
        found = []
        for lesson in self.list():
            if not lesson["enabled"] or lesson["id"] in exclude:
                continue
            score = self._score(q, lesson)
            if score >= min_score:
                found.append({**lesson, "score": round(score, 3)})
        found.sort(key=lambda x: -x["score"])
        return found[:limit]

    def stats(self) -> dict:
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(uses),0), COALESCE(SUM(wins),0) FROM lessons").fetchone()
        return {"lessons": row[0], "uses": row[1], "wins": row[2]}

    # ------------------------------------------------------------ запись

    def add(self, kind: str, title: str, problem: str = "", solution: str = "", tags=(),
            source_ticket: int | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"Тип урока должен быть одним из: {', '.join(KINDS)}")
        title, problem, solution = scrub(title).strip(), scrub(problem).strip(), scrub(solution).strip()
        if not title:
            raise ValueError("У урока должен быть заголовок")
        tags = [t.strip().lower() for t in (tags.split(",") if isinstance(tags, str) else tags) if t.strip()]
        # Такой урок уже есть — усиливаем его, а не дублируем.
        q = tokens(f"{title} {problem}")
        for same in self.list(kind=kind):
            other = tokens(f"{same['title']} {same['problem']}")
            if q and other and len(q & other) / math.sqrt(len(q) * len(other)) >= 0.5:
                fields = {"seen": same["seen"] + 1, "tags": sorted(set(same["tags"]) | set(tags))}
                if kind != "manual" and solution and len(solution) >= len(same["solution"]) // 2:
                    fields["solution"] = solution
                return self.update(same["id"], **fields)
        ts = now()
        with self.db.lock, self.db.conn:
            cur = self.db.conn.execute(
                "INSERT INTO lessons (kind, title, problem, solution, tags, source_ticket, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, title, problem, solution, ",".join(tags), source_ticket, ts, ts))
        return self.get(cur.lastrowid)

    def update(self, lesson_id: int, **fields) -> dict | None:
        allowed = {"kind", "title", "problem", "solution", "tags", "enabled", "seen"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tags":
                value = ",".join(t.strip().lower() for t in (value.split(",") if isinstance(value, str) else value)
                                 if t.strip())
            if key == "kind" and value not in KINDS:
                raise ValueError("Неизвестный тип урока")
            if key == "enabled":
                value = 1 if value in (True, 1, "1", "true", "on") else 0
            if key in ("title", "problem", "solution"):
                value = scrub(str(value)).strip()
            sets.append(f"{key}=?")
            args.append(value)
        if sets:
            sets.append("updated_at=?")
            args += [now(), lesson_id]
            with self.db.lock, self.db.conn:
                self.db.conn.execute(f"UPDATE lessons SET {', '.join(sets)} WHERE id=?", args)
        return self.get(lesson_id)

    def delete(self, lesson_id: int) -> None:
        with self.db.lock, self.db.conn:
            self.db.conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))

    def mark_used(self, ticket_id: int, lesson_ids) -> None:
        with self.db.lock, self.db.conn:
            for lid in lesson_ids:
                cur = self.db.conn.execute(
                    "INSERT OR IGNORE INTO lesson_uses (ticket_id, lesson_id) VALUES (?, ?)", (ticket_id, lid))
                if cur.rowcount:
                    self.db.conn.execute("UPDATE lessons SET uses = uses + 1 WHERE id=?", (lid,))

    def mark_outcome(self, ticket_id: int, solved: bool) -> None:
        if not solved:
            return
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "UPDATE lessons SET wins = wins + 1 WHERE id IN "
                "(SELECT lesson_id FROM lesson_uses WHERE ticket_id=?)", (ticket_id,))

    # ------------------------------------------------------------ обмен с коллегами

    def export(self) -> str:
        keys = ("kind", "title", "problem", "solution", "tags", "uses", "wins")
        return json.dumps([{k: les[k] for k in keys} for les in self.list()], ensure_ascii=False, indent=2)

    def import_(self, items: list[dict]) -> int:
        added = 0
        for item in items:
            try:
                self.add(item.get("kind", "manual"), item.get("title", ""), item.get("problem", ""),
                         item.get("solution", ""), item.get("tags", []))
                added += 1
            except (ValueError, AttributeError):
                continue
        return added


def prompt_block(lessons: list[dict]) -> str:
    """Опыт для промта: коротко, чтобы не раздувать контекст."""
    if not lessons:
        return ""
    lines = []
    for les in lessons:
        stat = f" (helped in {les['wins']} of {les['uses']} tickets)" if les["uses"] else ""
        lines.append(f"- [{les['kind']}] {les['title']}{stat}")
        if les["problem"]:
            lines.append(f"  when: {les['problem'][:300]}")
        if les["solution"]:
            lines.append(f"  do: {les['solution'][:500]}")
    return "\n".join(lines)
