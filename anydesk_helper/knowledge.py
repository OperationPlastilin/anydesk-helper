"""База опыта: уроки из прошлых тикетов — что сработало, какие были ошибки и как их чинили.

Как агент учится:
1. После сеанса модель разбирает ход решения и формулирует 0–3 урока (рефлексия).
2. Похожие уроки не плодятся: новый урок сливается с самым похожим существующим (seen += 1).
   Сохранённое решение при этом не затирается; другое решение той же проблемы (ни одного
   общего слова) — отдельный урок. Отключённый техником урок не включается снова.
3. Перед планом нового тикета агент ищет похожий опыт и кладёт его в промт.
4. Когда команда падает с ошибкой, агент ищет урок по тексту ошибки и подсказывает модели.
5. Счётчики uses/wins: сколько раз урок подсказывали и сколько из этих тикетов решено
   (каждый тикет — не больше одного раза) — полезные уроки поднимаются в поиске выше.
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
CREATE TABLE IF NOT EXISTS lesson_wins (
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
KEY_RE = re.compile(r"\b[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}\b")  # ключи продуктов
# Значение целиком: строка в кавычках или одно «слово».
VALUE = r"""(?:'[^'\n]*'|"[^"\n]*"|\S+)"""
HIDDEN = "<скрыто>"
# -Password 'X', password=X, пароль: X, /pass:X … Значения $pw и (Read-Host …) — не секрет,
# а (ConvertTo-SecureString 'X' …) скрывает SECURE_RE: рецепт остаётся рабочим.
# Ключ и значение — на одной строке: [ \t]*, а не \s* (иначе съедается команда со следующей).
SECRET_RE = re.compile(r"(?i)(-password\b:?|password[ \t]*[:=]|пароль[ \t]*[:=]|pidkey[ \t]*=|/inpkey:|"
                       r"/pass(?:word)?:)[ \t]*(?![($]|'<скрыто>')" + VALUE)
SECURE_RE = re.compile(r"""(?i)(ConvertTo-SecureString\b[^'"\n|;)]*?)(?:'[^'\n]*'|"[^"\n]*")""")
PIPE_SECURE_RE = re.compile(r"""(?i)('[^'\n]*'|"[^"\n]*")([ \t]*\|[ \t]*ConvertTo-SecureString\b)""")
VAR_RE = re.compile(r"""(?i)(\$\w*(?:pass|pw|cred|parol|secret)\w*\s*=\s*)(?:'[^'\n]*'|"[^"\n]*")""")
# ([adsi]'WinNT://./ivan,user').SetPassword('X'), .ChangePassword('old','new'), .Invoke("SetPassword", "X")
ADSI_RE = re.compile(r"""(?i)(\.(?:Set|Change)Password\(|\.Invoke\([ \t]*['"](?:Set|Change)Password['"][ \t]*,)"""
                     r"""([^)\n]*)""")
QUOTED_RE = re.compile(r"""'[^'\n]*'|"[^"\n]*["]""")
# Автовход: reg add …\Winlogon /v DefaultPassword /d X, -Name DefaultPassword -Value 'X', "DefaultPassword"="X"
AUTOLOGON_RE = re.compile(r"""(?i)(DefaultPassword\b(?:"?[ \t]*=[ \t]*|[^\n;|]*?(?:/d|-Value)[ \t]+))(?![($])"""
                          + VALUE)
# net user ivan Qwerty123 /add, net use Z: \\srv\share [/user:corp\ivan] Qwerty123
# (а «net user ivan *», «/active:yes» и русское слово после имени — «net user ivan что …» — нет)
NET_RE = re.compile(r"""(?i)\b(net[ \t]+user[ \t]+(?:"[^"\n]+"|[^\s/"]+)[ \t]+|"""
                    r"""net[ \t]+use[ \t]+(?:(?:[a-z]:|\*)[ \t]+)?\\\\\S+[ \t]+"""
                    r"""(?:/(?:user|u|persistent):\S+[ \t]+)*)"""
                    r"""(?![/*|;>&]|\d?>|[а-яё]+(?:\s|$|[.,;:!?]))""" + VALUE)
SHORT = {"1с", "ip", "ad"}  # значимые слова короче трёх букв
CODE_RE = re.compile(r"0x[0-9a-f]{6,8}|kb\d{6,8}")  # коды ошибок и номера обновлений


def tokens(text: str) -> set[str]:
    """Слова текста с грубым «стеммингом» (первые 6 букв): «принтера» ~ «принтер».
    Слова с цифрами (0x80070005, KB5034441) — целиком, ё = е, 1C (латиница) = 1С."""
    words = re.findall(r"[a-zа-я0-9][a-zа-я0-9_-]+", (text or "").lower().replace("ё", "е"))
    words = ("1с" if w == "1c" else w for w in words)
    return {w if any(c.isdigit() for c in w) else w[:6]
            for w in words if w not in STOP and (len(w) >= 3 or w in SHORT)}


def _cosine(a: set[str], b: set[str]) -> float:
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def _other_code(a: set[str], b: set[str]) -> bool:
    """В обоих текстах есть коды ошибок (или номера KB), но разные: другая ошибка — другой урок."""
    for prefix in ("0x", "kb"):
        ca, cb = ({t for t in x if t.startswith(prefix) and CODE_RE.fullmatch(t)} for x in (a, b))
        if ca and cb and not ca & cb:
            return True
    return False


def looks_like_error(output: str, exit_code) -> bool:
    return bool(exit_code) or bool(ERROR_RE.search(output or ""))


def scrub(text: str) -> str:
    """Уроки не должны хранить персональные данные и секреты пользователей."""
    text = EMAIL_RE.sub("<email>", text or "")
    text = KEY_RE.sub("<ключ>", text)
    text = SECURE_RE.sub(lambda m: m.group(1) + f"'{HIDDEN}'", text)
    text = PIPE_SECURE_RE.sub(lambda m: f"'{HIDDEN}'" + m.group(2), text)
    text = VAR_RE.sub(lambda m: m.group(1) + f"'{HIDDEN}'", text)
    text = ADSI_RE.sub(lambda m: m.group(1) + QUOTED_RE.sub(f"'{HIDDEN}'", m.group(2)), text)
    text = AUTOLOGON_RE.sub(lambda m: m.group(1) + HIDDEN, text)
    text = NET_RE.sub(lambda m: m.group(1) + HIDDEN, text)
    return SECRET_RE.sub(lambda m: m.group(1) + " " + HIDDEN, text)


class KnowledgeBase:
    def __init__(self, db):
        self.db = db
        with db.lock, db.conn:
            fresh = not db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lesson_wins'").fetchone()
            db.conn.executescript(SCHEMA)
            if fresh:  # база старой версии: решённые тикеты уже засчитаны в wins
                db.conn.execute("INSERT OR IGNORE INTO lesson_wins SELECT u.ticket_id, u.lesson_id "
                                "FROM lesson_uses u JOIN tickets t ON t.id = u.ticket_id "
                                "WHERE t.status = 'solved'")

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
        # Коды ошибок сверяем только по описанию: в решении бывают значения реестра (0x00000000).
        head = tokens(" ".join([lesson["title"], lesson["problem"], *lesson["tags"]]))
        if _other_code(q, head):
            return 0.0
        body = head | {t for t in tokens(lesson["solution"]) if len(t) >= 4}
        return _cosine(q, body) * (1 + 0.1 * min(lesson["wins"], 5))

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
            source_ticket: int | None = None, dedup: bool = True) -> dict:
        """dedup=False — урок, введённый техником вручную: всегда отдельной записью."""
        return self._add(kind, title, problem, solution, tags, source_ticket, dedup)[0]

    def _add(self, kind, title, problem="", solution="", tags=(), source_ticket=None,
             dedup=True) -> tuple[dict, bool]:
        """Возвращает (урок, слит ли он с уже существующим)."""
        if kind not in KINDS:
            raise ValueError(f"Тип урока должен быть одним из: {', '.join(KINDS)}")
        title, problem, solution = scrub(title).strip(), scrub(problem).strip(), scrub(solution).strip()
        if not title:
            raise ValueError("У урока должен быть заголовок")
        tags = tags.split(",") if isinstance(tags, str) else tags if isinstance(tags, (list, tuple, set)) else ()
        tags = [str(t).strip().lower() for t in tags if str(t).strip()]
        # Такой урок уже есть — усиливаем самый похожий, а не дублируем.
        q, sol = tokens(f"{title} {problem}"), tokens(solution)
        score, best = 0.0, None
        for same in self.list(kind=kind) if dedup else ():
            other = tokens(f"{same['title']} {same['problem']}")
            s = _cosine(q, other)
            # Другой код ошибки или решение без единого общего слова — другая причина, другой урок.
            if s > score and not _other_code(q, other) and not (
                    sol and same["solution"] and not sol & tokens(same["solution"])):
                score, best = s, same
        if best and score >= 0.5:
            # Решение не затираем: в нём проверенный рецепт (его wins) или правка техника.
            # Отключённый урок остаётся отключённым — агент не «переучивается» на отвергнутом.
            fields = {"seen": best["seen"] + 1, "tags": sorted(set(best["tags"]) | set(tags))}
            if solution and not best["solution"]:
                fields["solution"] = solution
            return self.update(best["id"], **fields), True
        ts = now()
        with self.db.lock, self.db.conn:
            cur = self.db.conn.execute(
                "INSERT INTO lessons (kind, title, problem, solution, tags, source_ticket, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, title, problem, solution, ",".join(tags), source_ticket, ts, ts))
        return self.get(cur.lastrowid), False

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
        # Повторный запуск решённого тикета не добавляет урокам ещё одну «победу».
        with self.db.lock, self.db.conn:
            for (lid,) in self.db.conn.execute(
                    "SELECT lesson_id FROM lesson_uses WHERE ticket_id=?", (ticket_id,)).fetchall():
                cur = self.db.conn.execute(
                    "INSERT OR IGNORE INTO lesson_wins (ticket_id, lesson_id) VALUES (?, ?)", (ticket_id, lid))
                if cur.rowcount:
                    self.db.conn.execute("UPDATE lessons SET wins = wins + 1 WHERE id=?", (lid,))

    # ------------------------------------------------------------ обмен с коллегами

    def export(self) -> str:
        keys = ("kind", "title", "problem", "solution", "tags", "enabled", "uses", "wins")
        return json.dumps([{k: les[k] for k in keys} for les in self.list()], ensure_ascii=False, indent=2)

    def import_(self, items: list[dict]) -> int:
        """Возвращает число новых уроков: слитые с похожими и точные повторы не считаются."""
        added = 0
        for item in items:
            try:
                kind = str(item.get("kind") or "manual")
                title, problem, solution = (scrub(str(item.get(k) or "")).strip()
                                            for k in ("title", "problem", "solution"))
                # Свой же экспорт (восстановление из копии) не «усиливает» уроки повторно.
                if any((les["title"], les["problem"], les["solution"]) == (title, problem, solution)
                       for les in self.list(kind=kind)):
                    continue
                # Отключённый урок переносим как есть и с чужими уроками не сливаем.
                disabled = item.get("enabled") is False
                lesson, merged = self._add(kind, title, problem, solution, item.get("tags") or (),
                                           dedup=not disabled)
            except (ValueError, AttributeError, TypeError):
                continue
            if disabled:
                self.update(lesson["id"], enabled=False)
            added += not merged
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
