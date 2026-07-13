"""Shared sqlite helper: sqlite3's own context manager commits but never closes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def sqlite_conn(path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
