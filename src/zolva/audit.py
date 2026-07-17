"""Audit plugin: hash-chained, append-only log of every bus step + the SARR scorecard.

Each row's hash covers the previous row's hash, so silent edits and deletions
are detectable (`AuditLog.verify()`). Storage sits behind the small
`AuditStore` protocol: SQLite ships as the default, and a bank backs it with
Postgres (or WORM object storage) by implementing the same four methods,
the chain logic never changes. `InMemoryAuditStore` is the reference second
implementation and doubles as a unit-test double.

`verify()` defaults to the full pass from genesis (the regulator-grade
check). Monitors polling every few seconds pass `incremental=True` to hash
only rows past the last proven one, paired with a periodic full pass, which
is exactly what the dashboard does.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

from zolva._db import sqlite_conn
from zolva.bus import Step, Verdict
from zolva.orchestrator import AgentApp

_GENESIS = "genesis"

# eval/synthetic traffic uses these session-id prefixes; production evidence
# (scorecard, failure queue) must not be polluted by test runs against a
# live app instance
NON_PRODUCTION_SESSION_PREFIXES = ("eval-", "synthetic-")

# (id, ts, session_id, agent, type, data, prev_hash, hash)
AuditRow = tuple[int, str, str, str, str, str, str, str]
# (ts, session_id, agent, type, data, prev_hash, hash) — id is storage-assigned
_NewRow = tuple[str, str, str, str, str, str, str]


class AuditStore(Protocol):
    """What a storage backend must provide. Postgres recipe: same four
    methods over one table; append_chained must be atomic (SELECT ... FOR
    UPDATE or a serializable transaction around read-last-hash + insert)."""

    def append_chained(self, build: Callable[[str], _NewRow]) -> None:
        """Atomically read the last row's hash and insert build(prev_hash)."""
        ...

    def rows(self, after_id: int = 0) -> list[AuditRow]: ...

    def row(self, row_id: int) -> AuditRow | None: ...

    def session_types(self) -> list[tuple[str, str]]:
        """(session_id, step type) for every row; feeds the scorecard."""
        ...


class SqliteAuditStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                "session_id TEXT NOT NULL, agent TEXT NOT NULL, type TEXT NOT NULL, "
                "data TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"
            )
            # session lookups and per-session grouping (scorecard, dashboard)
            # must not full-scan a log that only ever grows
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id)")

    def _conn(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        return sqlite_conn(self._path, immediate=immediate)

    def append_chained(self, build: Callable[[str], _NewRow]) -> None:
        with self._conn(immediate=True) as conn:
            row = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else _GENESIS
            conn.execute(
                "INSERT INTO audit (ts, session_id, agent, type, data, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                build(prev),
            )

    def rows(self, after_id: int = 0) -> list[AuditRow]:
        with self._conn() as conn:
            return [
                (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                for r in conn.execute(
                    "SELECT id, ts, session_id, agent, type, data, prev_hash, hash "
                    "FROM audit WHERE id > ? ORDER BY id",
                    (after_id,),
                )
            ]

    def row(self, row_id: int) -> AuditRow | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT id, ts, session_id, agent, type, data, prev_hash, hash "
                "FROM audit WHERE id = ?",
                (row_id,),
            ).fetchone()
        return (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) if r else None

    def session_types(self) -> list[tuple[str, str]]:
        with self._conn() as conn:
            return [(r[0], r[1]) for r in conn.execute("SELECT session_id, type FROM audit")]


class InMemoryAuditStore:
    """Reference AuditStore: proves the chain logic is storage-independent
    and gives unit tests a backend with no filesystem."""

    def __init__(self) -> None:
        self._rows: list[AuditRow] = []

    def append_chained(self, build: Callable[[str], _NewRow]) -> None:
        prev = self._rows[-1][7] if self._rows else _GENESIS
        self._rows.append((len(self._rows) + 1, *build(prev)))

    def rows(self, after_id: int = 0) -> list[AuditRow]:
        return [r for r in self._rows if r[0] > after_id]

    def row(self, row_id: int) -> AuditRow | None:
        return next((r for r in self._rows if r[0] == row_id), None)

    def session_types(self) -> list[tuple[str, str]]:
        return [(r[2], r[4]) for r in self._rows]


def _digest(prev: str, ts: str, session_id: str, agent: str, step_type: str, data: str) -> str:
    return hashlib.sha256(
        f"{prev}|{ts}|{session_id}|{agent}|{step_type}|{data}".encode()
    ).hexdigest()


class AuditLog:
    def __init__(self, path_or_store: str | Path | AuditStore) -> None:
        if isinstance(path_or_store, (str, Path)):
            self._store: AuditStore = SqliteAuditStore(path_or_store)
        else:
            self._store = path_or_store
        self._attached = False
        self._checkpoint: tuple[int, str] | None = None  # (row id, hash) proven so far

    def attach(self, app: AgentApp) -> None:
        if self._attached:
            return  # idempotent: a second attach must not double-log every step
        self._attached = True
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        self.append(step)
        return None

    def append(self, step: Step, *, ts: str | None = None) -> None:
        """`ts` override is for backfill/import (e.g. demo seeders); live steps
        always stamp now. Forged timestamps are still chain-covered."""
        row_ts = ts if ts is not None else datetime.now(timezone.utc).isoformat()
        payload = json.dumps(step.data, sort_keys=True, default=str)

        def build(prev: str) -> _NewRow:
            digest = _digest(prev, row_ts, step.session_id, step.agent, step.type, payload)
            return (row_ts, step.session_id, step.agent, step.type, payload, prev, digest)

        self._store.append_chained(build)

    def verify(self, *, incremental: bool = False) -> bool:
        """Recompute the chain; any edited, deleted, or reordered row breaks it.

        Default is the full pass from genesis, the regulator-grade check.
        `incremental=True` hashes only rows past this instance's last proven
        one (after re-checking that the boundary row is unchanged), which is
        what a monitor polling every few seconds should use, paired with a
        periodic full pass; an edit BEFORE the proven boundary is only caught
        by the full pass. Any boundary anomaly falls back to full."""
        prev = _GENESIS
        after_id = 0
        if incremental and self._checkpoint is not None:
            cid, chash = self._checkpoint
            boundary = self._store.row(cid)
            # re-HASH the boundary, comparing the stored hash column alone
            # would miss a rewrite of the boundary row's own payload
            if (
                boundary is not None
                and boundary[7] == chash
                and _digest(
                    boundary[6], boundary[1], boundary[2], boundary[3], boundary[4], boundary[5]
                )
                == chash
            ):
                prev, after_id = chash, cid
            else:
                self._checkpoint = None  # boundary rewritten or gone: full re-verify
        rows = self._store.rows(after_id=after_id)
        last: tuple[int, str] | None = self._checkpoint if after_id else None
        for row_id, ts, session_id, agent, step_type, data, prev_hash, digest in rows:
            if prev_hash != prev or digest != _digest(prev, ts, session_id, agent, step_type, data):
                # once broken, stay red: a later incremental call must not
                # trust a checkpoint that predates a detected break
                self._checkpoint = None
                return False
            prev = digest
            last = (row_id, digest)
        self._checkpoint = last
        return True

    def step_types_by_session(self) -> dict[str, set[str]]:
        by_session: dict[str, set[str]] = {}
        for session_id, step_type in self._store.session_types():
            by_session.setdefault(session_id, set()).add(step_type)
        return by_session


class Scorecard(BaseModel):
    sessions: int
    resolved: int
    escalated: int
    sarr: float  # Safe Automated Resolution Rate
    containment: float

    def summary(self) -> str:
        return (
            f"sessions={self.sessions}  resolved={self.resolved}  escalated={self.escalated}\n"
            f"SARR={self.sarr:.1%}  containment={self.containment:.1%}"
        )


def scorecard(
    audit: AuditLog,
    *,
    exclude_session_prefixes: tuple[str, ...] = NON_PRODUCTION_SESSION_PREFIXES,
) -> Scorecard:
    """SARR v1: session got a response and never escalated. Eval and
    synthetic sessions are excluded by default, a patrol run against the
    production app must not move the production metric.

    ponytail: no re-contact window yet (needs customer identity across
    sessions); add when a bank wires customer refs into session ids.
    """
    by_session = audit.step_types_by_session()
    sessions = [
        types
        for sid, types in by_session.items()
        if "user_msg" in types and not sid.startswith(exclude_session_prefixes)
    ]
    total = len(sessions)
    escalated = sum(1 for types in sessions if "handover" in types)
    resolved = sum(1 for types in sessions if "response" in types and "handover" not in types)
    return Scorecard(
        sessions=total,
        resolved=resolved,
        escalated=escalated,
        sarr=resolved / total if total else 0.0,
        containment=1 - (escalated / total) if total else 0.0,
    )
