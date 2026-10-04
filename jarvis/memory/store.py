"""SQLite memory: conversation history + notes/facts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
"""


class Memory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config.DATA_DIR / "jarvis.db")
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
        return self._conn

    # ---------------------------------------------------------- messages
    def add_message(self, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.conn().execute("INSERT INTO messages(ts, role, content, meta) VALUES (?,?,?,?)",
                                (time.time(), role, content, json.dumps(meta or {})))
            self.conn().commit()

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn().execute(
                "SELECT ts, role, content, meta FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r["ts"], "role": r["role"], "content": r["content"], "meta": json.loads(r["meta"] or "{}")}
                for r in reversed(rows)]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn().execute(
                "SELECT ts, role, content FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def clear_history(self) -> None:
        with self._lock:
            self.conn().execute("DELETE FROM messages")
            self.conn().commit()

    # ---------------------------------------------------------- notes
    def add_note(self, text: str, kind: str = "note") -> int:
        with self._lock:
            cur = self.conn().execute("INSERT INTO notes(ts, kind, text) VALUES (?,?,?)",
                                      (time.time(), kind, text))
            self.conn().commit()
            return int(cur.lastrowid or 0)

    def notes(self, kind: str | None = None, query: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        sql = "SELECT id, ts, kind, text FROM notes WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if query:
            sql += " AND text LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn().execute(sql, args).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "text": r["text"],
                 "date": time.strftime("%Y-%m-%d", time.localtime(r["ts"]))} for r in rows]

    def delete_note(self, note_id: int) -> bool:
        with self._lock:
            cur = self.conn().execute("DELETE FROM notes WHERE id = ?", (note_id,))
            self.conn().commit()
            return cur.rowcount > 0

    def messages_since(self, last_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn().execute(
                "SELECT id, ts, role, content FROM messages WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def facts_for_prompt(self, limit: int = 25) -> str:
        from . import vault

        try:
            text = vault.for_prompt(limit)
        except Exception:
            text = ""
        rows = self.notes("fact", None, limit)  # older installs kept facts in SQLite
        extra = "\n".join(f"- {r['text']}" for r in rows)
        return "\n".join(x for x in (text, extra) if x)


memory = Memory()
