"""Тикеты и ход их решения в SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    anydesk_id  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    summary     TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    data      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ticket ON events(ticket_id, id);
"""

STATUSES = ("new", "running", "waiting", "paused", "solved", "failed", "stopped")
EDITABLE = ("title", "description", "anydesk_id", "status", "summary")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        # Если программу закрыли посреди сеанса — такие тикеты больше не выполняются.
        with self.lock, self.conn:
            self.conn.execute("UPDATE tickets SET status='stopped' "
                              "WHERE status IN ('running','waiting','paused')")

    @staticmethod
    def _ticket(row) -> dict | None:
        if row is None:
            return None
        t = dict(row)
        t["plan"] = json.loads(t["plan"] or "[]")
        return t

    def create_ticket(self, title: str, description: str = "", anydesk_id: str = "") -> dict:
        ts = now()
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO tickets (title, description, anydesk_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (title, description, anydesk_id, ts, ts))
        return self.get_ticket(cur.lastrowid)

    def get_ticket(self, ticket_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return self._ticket(row)

    def list_tickets(self, query: str = "", status: str = "") -> list[dict]:
        sql = "SELECT * FROM tickets WHERE 1=1"
        args: list = []
        if query:
            sql += " AND (title LIKE ? OR description LIKE ? OR anydesk_id LIKE ? OR CAST(id AS TEXT)=?)"
            like = f"%{query}%"
            args += [like, like, like, query.lstrip("#")]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT 500"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._ticket(r) for r in rows]

    def update_ticket(self, ticket_id: int, **fields) -> dict | None:
        sets, args = [], []
        for key, value in fields.items():
            if key == "plan":
                value = json.dumps(value, ensure_ascii=False)
            elif key not in EDITABLE:
                raise ValueError(f"Поле {key} нельзя менять")
            if key == "status" and value not in STATUSES:
                raise ValueError(f"Неизвестный статус {value}")
            sets.append(f"{key}=?")
            args.append(value)
        if sets:
            sets.append("updated_at=?")
            args += [now(), ticket_id]
            with self.lock, self.conn:
                self.conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", args)
        return self.get_ticket(ticket_id)

    def delete_ticket(self, ticket_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))

    def add_event(self, ticket_id: int, kind: str, text: str = "", **data) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ticket_id, ts, kind, text, data) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, now(), kind, text, json.dumps(data, ensure_ascii=False, default=str)))
            self.conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now(), ticket_id))
        return cur.lastrowid

    def events(self, ticket_id: int, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE ticket_id=? AND id>? ORDER BY id", (ticket_id, after)
            ).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["data"] = json.loads(e["data"] or "{}")
            out.append(e)
        return out
