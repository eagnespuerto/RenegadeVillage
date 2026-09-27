"""SQLite storage for channels, messages and per-agent state."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    topic TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    author TEXT NOT NULL,
    author_kind TEXT NOT NULL,          -- 'agent' | 'human' | 'system'
    content TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, id);
CREATE TABLE IF NOT EXISTS agent_state (
    name TEXT PRIMARY KEY,
    last_seen INTEGER NOT NULL DEFAULT 0,
    memory TEXT NOT NULL DEFAULT ''
);
"""


def normalize_channel(name: str) -> str:
    name = name.strip().lstrip("#").lower()
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"[^a-z0-9-]", "", name)
    return name.strip("-")[:40]


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # channels -------------------------------------------------------------
    def ensure_channel(self, name: str, topic: str = "", created_by: str = "system") -> tuple[str, bool]:
        """Create the channel if needed. Returns (normalized name, created?)."""
        norm = normalize_channel(name)
        if not norm:
            raise ValueError(f"invalid channel name: {name!r}")
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO channels(name, topic, created_by, created_at) VALUES (?,?,?,?)",
                (norm, topic, created_by, time.time()),
            )
            self._db.commit()
            return norm, cur.rowcount == 1

    def channel_exists(self, name: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM channels WHERE name=?", (name,)).fetchone() is not None

    def list_channels(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM channels ORDER BY created_at, name").fetchall()
        return [dict(r) for r in rows]

    # messages -------------------------------------------------------------
    def add_message(self, channel: str, author: str, author_kind: str, content: str,
                    attachments: list[str] | None = None) -> dict:
        now = time.time()
        att = json.dumps(attachments or [])
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages(channel, author, author_kind, content, attachments, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (channel, author, author_kind, content, att, now),
            )
            self._db.commit()
            mid = cur.lastrowid
        return {"id": mid, "channel": channel, "author": author, "author_kind": author_kind,
                "content": content, "attachments": attachments or [], "created_at": now}

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["attachments"] = json.loads(d["attachments"])
        return d

    def get_messages(self, channel: str, limit: int = 50, before: int | None = None) -> list[dict]:
        q = "SELECT * FROM messages WHERE channel=?"
        args: list = [channel]
        if before:
            q += " AND id<?"
            args.append(before)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def recent_messages(self, limit: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def rename_human(self, old: str, new: str) -> None:
        with self._lock:
            self._db.execute("UPDATE messages SET author=? WHERE author=? AND author_kind='human'", (new, old))
            self._db.commit()

    def max_message_id(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    # agent state ----------------------------------------------------------
    def agent_state(self, name: str) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM agent_state WHERE name=?", (name,)).fetchone()
        return dict(r) if r else {"name": name, "last_seen": 0, "memory": ""}

    def set_agent_state(self, name: str, *, last_seen: int | None = None, memory: str | None = None) -> None:
        st = self.agent_state(name)
        if last_seen is not None:
            st["last_seen"] = last_seen
        if memory is not None:
            st["memory"] = memory
        with self._lock:
            self._db.execute(
                "INSERT INTO agent_state(name, last_seen, memory) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, memory=excluded.memory",
                (name, st["last_seen"], st["memory"]),
            )
            self._db.commit()
