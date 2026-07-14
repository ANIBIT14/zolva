"""Session storage. Isolation per session_id is a security property, not a convenience."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from zolva._db import sqlite_conn
from zolva.bridge import Message


class SessionStore(Protocol):
    async def history(self, session_id: str) -> list[Message]: ...

    async def append(self, session_id: str, messages: list[Message]) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}

    async def history(self, session_id: str) -> list[Message]:
        return list(self._sessions.get(session_id, []))

    async def append(self, session_id: str, messages: list[Message]) -> None:
        self._sessions.setdefault(session_id, []).extend(messages)


class SqliteSessionStore:
    # ponytail: sync sqlite3 behind async methods; swap for aiosqlite if contention ever measured
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS messages "
                "(session_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (session_id, seq))"
            )

    def _conn(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        return sqlite_conn(self._path, immediate=immediate)

    async def history(self, session_id: str) -> list[Message]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [Message.model_validate_json(r[0]) for r in rows]

    async def append(self, session_id: str, messages: list[Message]) -> None:
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            next_seq = int(row[0]) + 1
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?)",
                [(session_id, next_seq + i, m.model_dump_json()) for i, m in enumerate(messages)],
            )
