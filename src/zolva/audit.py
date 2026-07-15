"""Audit plugin: hash-chained, append-only log of every bus step + the SARR scorecard.

Each row's hash covers the previous row's hash, so silent edits and deletions
are detectable (`AuditLog.verify()`). SQLite by default; point the path at
WORM-backed storage for regulators.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from zolva._db import sqlite_conn
from zolva.bus import Step, Verdict
from zolva.orchestrator import AgentApp

_GENESIS = "genesis"


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._attached = False
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                "session_id TEXT NOT NULL, agent TEXT NOT NULL, type TEXT NOT NULL, "
                "data TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"
            )

    def _conn(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        return sqlite_conn(self._path, immediate=immediate)

    def attach(self, app: AgentApp) -> None:
        if self._attached:
            return  # idempotent: a second attach must not double-log every step
        self._attached = True
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        self.append(step)
        return None

    def append(self, step: Step) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(step.data, sort_keys=True, default=str)
        with self._conn(immediate=True) as conn:
            row = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else _GENESIS
            digest = hashlib.sha256(
                f"{prev}|{ts}|{step.session_id}|{step.agent}|{step.type}|{payload}".encode()
            ).hexdigest()
            conn.execute(
                "INSERT INTO audit (ts, session_id, agent, type, data, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, step.session_id, step.agent, step.type, payload, prev, digest),
            )

    def verify(self) -> bool:
        """Recompute the chain; any edited, deleted, or reordered row breaks it."""
        prev = _GENESIS
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, session_id, agent, type, data, prev_hash, hash FROM audit ORDER BY id"
            ).fetchall()
        for ts, session_id, agent, step_type, data, prev_hash, digest in rows:
            if prev_hash != prev:
                return False
            expected = hashlib.sha256(
                f"{prev}|{ts}|{session_id}|{agent}|{step_type}|{data}".encode()
            ).hexdigest()
            if digest != expected:
                return False
            prev = digest
        return True

    def step_types_by_session(self) -> dict[str, set[str]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT session_id, type FROM audit").fetchall()
        by_session: dict[str, set[str]] = {}
        for session_id, step_type in rows:
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


def scorecard(audit: AuditLog) -> Scorecard:
    """SARR v1: session got a response and never escalated.

    ponytail: no re-contact window yet (needs customer identity across
    sessions); add when a bank wires customer refs into session ids.
    """
    by_session = audit.step_types_by_session()
    sessions = [types for sid, types in by_session.items() if "user_msg" in types]
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
