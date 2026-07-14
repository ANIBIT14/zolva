"""Shared sqlite helper: explicit transaction control (commit on success,
rollback on error, always closes); `immediate=True` takes the write lock
up front for read-then-write appends."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def sqlite_conn(path: str, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """immediate=True takes the write lock up front — required for
    read-then-write appends (audit chain, session seq) shared across processes."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
