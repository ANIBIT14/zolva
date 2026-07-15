import sqlite3
from pathlib import Path

import pytest

from zolva._db import sqlite_conn


def test_immediate_conn_holds_write_lock(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    with sqlite_conn(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    cm = sqlite_conn(db, immediate=True)
    with cm as conn:
        conn.execute("INSERT INTO t VALUES (1)")
        other = sqlite3.connect(db, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_default_conn_commits_and_closes(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    with sqlite_conn(db) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    with sqlite_conn(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_rollback_on_error(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    with sqlite_conn(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(RuntimeError):
        with sqlite_conn(db, immediate=True) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    with sqlite_conn(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
