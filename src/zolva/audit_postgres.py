"""Postgres `AuditStore`: the production backend for banks that need real
retention (EU AI Act Art. 12 mandates >= 6 months) and multi-instance access
that a single SQLite file cannot serve.

The chain logic in `audit.py` is storage-independent — this file only supplies
the four `AuditStore` methods over one table. The one thing that must be right
is `append_chained`'s atomicity: read-last-hash and insert have to be
serialized across every app instance, or two concurrent appends fork the
chain. A transaction-scoped advisory lock (`pg_advisory_xact_lock`) gives that
across processes; it is released automatically when the transaction ends.

Requires the optional extra: `pip install "zolva[postgres]"`.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from zolva.audit import _GENESIS, AuditRow, _NewRow

# arbitrary but fixed key: every appender contends on the same advisory lock
_APPEND_LOCK_KEY = 128009511  # any constant works; all appenders must share it

_COLS = "id, ts, session_id, agent, type, data, prev_hash, hash"


class PostgresAuditStore:
    def __init__(self, dsn: str, *, table: str = "audit") -> None:
        import psycopg  # lazy: only banks using this backend need the driver

        if not table.isidentifier():
            raise ValueError(f"invalid table name: {table!r}")
        self._table = table
        self._conn = psycopg.connect(dsn, autocommit=True)
        # ponytail: single connection guarded by a lock (audit appends are
        # serialized by the advisory lock anyway); swap to psycopg_pool if a
        # bank's read throughput on the dashboard outgrows one connection.
        self._lock = threading.Lock()
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT NOT NULL, "
                "agent TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, "
                "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"
            )
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_session ON {table}(session_id)")

    def append_chained(self, build: Callable[[str], _NewRow]) -> None:
        with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_APPEND_LOCK_KEY,))
            # nosec B608: table is isidentifier-validated in __init__ and _COLS is a
            # module constant; only values are ever passed as bound %s parameters
            cur.execute(f"SELECT hash FROM {self._table} ORDER BY id DESC LIMIT 1")  # nosec B608
            row = cur.fetchone()
            prev = row[0] if row else _GENESIS
            cur.execute(
                f"INSERT INTO {self._table} "  # nosec B608: validated identifier, values bound
                "(ts, session_id, agent, type, data, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                build(prev),
            )

    def rows(self, after_id: int = 0) -> list[AuditRow]:
        with self._lock, self._conn.cursor() as cur:
            # nosec B608: validated identifier, constant columns, value bound as %s
            cur.execute(
                f"SELECT {_COLS} FROM {self._table} WHERE id > %s ORDER BY id",  # nosec B608
                (after_id,),
            )
            return [self._tuple(r) for r in cur.fetchall()]

    def row(self, row_id: int) -> AuditRow | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM {self._table} WHERE id = %s", (row_id,))  # nosec B608
            r = cur.fetchone()
        return self._tuple(r) if r else None

    def session_types(self) -> list[tuple[str, str]]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT session_id, type FROM {self._table}")  # nosec B608
            return [(r[0], r[1]) for r in cur.fetchall()]

    @staticmethod
    def _tuple(r: Any) -> AuditRow:
        return (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
